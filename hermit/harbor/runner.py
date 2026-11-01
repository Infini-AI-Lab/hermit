"""`agent.name: harbor`: one Hermit rollout as one Harbor trial, or one step of a Harbor multi-step task.

    agent:
      name: harbor
      concurrency: 8
      params:
        benchmark: ../utils/benchmark.yaml   # optional: these same keys, shared by every method's config
        tasks_dir: ../utils/tasks            # the Harbor task directories (dataset.py exports or indexes them)
        model: gpt-5.6-luna                  # the acting model; a `<backend>/<model>` reference binds an endpoint
        acting_agent:                        # Harbor's agent in the container
          import_path: harborside.codex:HermitCodex   # default: Codex with the adapter deployed; or name: codex,
          kwargs: {}                                  # mini-swe-agent, ... (Harbor's built-ins; no adapter then)
          env: {}
          pythonpath: []                     # directories Harbor must import the agent from (an example's own)
        effort: medium                       # Codex: model_reasoning_effort
        codex_version: "0.164.0"             # Codex: the npm pin installed per trial
        codex_login: true                    # Codex: the ChatGPT login (~/.codex/auth.json), when no endpoint is bound
        agent_timeout_sec: 3600              # uniform agent budget; null: each task's own
        environment: {delete: true}          # Harbor's environment config (type, import_path, force_build, kwargs)
        verifier: {}                         # Harbor's verifier config (override_timeout_sec, ...)
        retry: {max_retries: 0}              # Harbor's retry of a failed trial (not of a stream's step)
        reward_key: reward                   # the verifier's reward the judge reads
        jobs_dir: ~/.cache/hermit/harbor/jobs   # one Harbor job per rollout, outside the checkout
        tmpdir: <jobs_dir>/tmp               # TMPDIR for harbor; never /tmp
        harbor: ~/.cache/hermit/harbor-venv/bin/harbor
        docker_cleanup: images_keep          # images_keep | rmi_task_image | rmi_over_gb (docker.py)
        rmi_threshold_gb: 1.0
        disk_gate_gb: 20                     # no trial starts while Docker's data root has less than this free
        disk_gate_wait_s: 7200               # ... for at most this long; then the rollout is infra (disk)
        stream: {idle_s: 14400, start_timeout_s: 3600}   # a multi-step task's driver: idle stop, environment start

A task row (`dataset.index`) names its directory as `harbor_task`; a step row adds `step`, `step_index`, `steps`.

A trial: the adapter is staged (`staging.stage`), the job config written, `harbor run -c job.yaml` run in
Harbor's own venv as a subprocess, and the trial's `result.json` read back (`errors.classify`). A step: the
stream's driver (`stream.StreamClient`, `harborside/driver.py`) is started with the first step and keeps the task's
environment; each rollout stages its adapter, waits for its turn, asks for its step and reads the step's result;
the last step (or an abort) stops the driver.

What the rollout returns: `output` {harbor_task, rollout, step, reward, rewards, status, failure_cause,
exception_type, trial, hooks_active}, `tokens` for an `ok` trial only, the curated log (`curate.py`), and
`extra` with the verdict, the job and what was deployed. A usage-limit or authentication failure writes
`<jobs_dir>/HALT`: while it exists every rollout returns at once as infra/halted, without a container; whoever
restored the login or waited for the window removes it.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import yaml

from hermit.agents.base import Agent
from hermit.harbor import curate as curate_mod
from hermit.harbor import docker
from hermit.harbor.errors import classify, no_trial
from hermit.harbor.staging import stage
from hermit.harbor.stream import DRIVER_LOG, StreamClient, StreamError, StreamOrderError
from hermit.task import RunOutput

HARBOR = "~/.cache/hermit/harbor-venv/bin/harbor"
JOBS_DIR = "~/.cache/hermit/harbor/jobs"
HERMIT_CODEX = "harborside.codex:HermitCodex"
HARBORSIDE_ROOT = Path(__file__).resolve().parent                # the directory holding the harborside package
SETUP_BUDGET_S = 360                                             # Harbor's agent setup: apt + nvm + npm + python3
TEARDOWN_BUDGET_S = 600                                          # image pull, container start, compose down
DEFAULT_AGENT_TIMEOUT_S = 3600
DISK_GATE_GB = 20.0
DISK_GATE_WAIT_S = 7200.0
HALT_FILE = "HALT"
NESTED = ("acting_agent", "environment", "verifier", "retry", "stream")   # params merged one level deep


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(text))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if key in NESTED and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


class HarborAgent(Agent):
    """`agent.name: harbor`."""

    def __init__(self, params: dict | None = None, base: Path | str | None = None):
        super().__init__(params, base)
        given = dict(self.params)
        if given.get("benchmark"):
            shared = yaml.safe_load(self.path(given["benchmark"]).read_text()) or {}
            if not isinstance(shared, dict):
                raise ValueError(f"agent.params.benchmark {given['benchmark']} is not a mapping")
            self.params = _merge(shared, {k: v for k, v in given.items() if k != "benchmark"})
            self.benchmark_base = self.path(given["benchmark"]).parent
        else:
            self.benchmark_base = self.base
        if not self.params.get("tasks_dir"):
            raise ValueError("agent.params.tasks_dir is required (the Harbor task directories)")
        self.docker_cleanup                                  # an unknown mode fails here, before any container
        self.acting_agent                                    # and an agent spec without a name

    # --- params ---------------------------------------------------------------------------------------------
    def _path(self, value) -> Path:
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.benchmark_base / p).resolve()

    @property
    def harbor(self) -> str:
        return str(Path(os.path.expanduser(str(self.params.get("harbor") or HARBOR))))

    @property
    def harbor_python(self) -> str:
        return str(Path(self.harbor).parent / "python")

    @property
    def tasks_dir(self) -> Path:
        return self._path(self.params["tasks_dir"])

    @property
    def jobs_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.params.get("jobs_dir") or JOBS_DIR))).resolve()

    @property
    def tmpdir(self) -> Path:
        value = self.params.get("tmpdir")
        return Path(os.path.expanduser(str(value))).resolve() if value else self.jobs_dir / "tmp"

    @property
    def python(self) -> str:
        return str(self.params.get("python") or sys.executable)

    @property
    def acting_agent(self) -> dict:
        spec = dict(self.params.get("acting_agent") or {})
        if not spec.get("name") and not spec.get("import_path"):
            spec["import_path"] = HERMIT_CODEX
        if spec.get("name") and spec.get("import_path"):
            raise ValueError("agent.params.acting_agent names both a name and an import_path")
        return spec

    @property
    def is_codex(self) -> bool:
        spec = self.acting_agent
        return spec.get("name") == "codex" or str(spec.get("import_path", "")).endswith(":HermitCodex")

    @property
    def deploys_adapter(self) -> bool:
        return str(self.acting_agent.get("import_path", "")).endswith(":HermitCodex")

    @property
    def agent_timeout_sec(self) -> int | None:
        value = self.params.get("agent_timeout_sec")
        return int(value) if value else None

    @property
    def docker_cleanup(self) -> str:
        mode = str(self.params.get("docker_cleanup") or "images_keep")
        if mode not in docker.CLEANUP_MODES:
            raise ValueError(f"agent.params.docker_cleanup must be one of {', '.join(docker.CLEANUP_MODES)}, not {mode!r}")
        return mode

    @property
    def reward_key(self) -> str:
        return str(self.params.get("reward_key") or "reward")

    def model_fields(self) -> dict:
        """Harbor's model fields from `model`: a name, or a bound backend's {model, base_url, api_key_env}."""
        model = self.params.get("model")
        if isinstance(model, dict):
            out = {"model_name": model.get("model"), "base_url": model.get("base_url"),
                   "model_api_key_name": model.get("api_key_env")}
            return {k: v for k, v in out.items() if v}
        return {"model_name": str(model)} if model else {}

    def task_dir(self, task: dict) -> Path:
        return self.tasks_dir / str(task.get("harbor_task") or task.get("task") or task["id"])

    def wait_budget(self, task: dict, *, build: bool = True) -> float:
        """How long a trial may take: setup, the agent's budget, the task's verifier budget, the image build
        (a Dockerfile task), teardown; or the explicit `wait_budget_s`."""
        if self.params.get("wait_budget_s"):
            return float(self.params["wait_budget_s"])
        agent = self.agent_timeout_sec or int(task.get("agent_timeout_sec") or DEFAULT_AGENT_TIMEOUT_S)
        verifier = int(task.get("verifier_timeout_sec") or agent)
        built = int(task.get("build_timeout_sec") or 0) if build and not task.get("docker_image") else 0
        return (agent + int(self.params.get("setup_budget_s") or SETUP_BUDGET_S) + verifier + built
                + int(self.params.get("teardown_budget_s") or TEARDOWN_BUDGET_S))

    # --- the job --------------------------------------------------------------------------------------------
    def agent_config(self, task: dict, staging: Path | None, deployed: dict | None) -> dict:
        """Harbor's agent config for this rollout."""
        spec = self.acting_agent
        agent: dict = {("import_path" if spec.get("import_path") else "name"): spec.get("import_path") or spec["name"]}
        agent.update(self.model_fields())
        if agent.get("base_url"):
            agent["skip_model_parsing"] = True              # a bound endpoint serves the id as given (accounts/.../model)
        if self.agent_timeout_sec:
            agent["override_timeout_sec"] = self.agent_timeout_sec
        kwargs = dict(spec.get("kwargs") or {})
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        if self.is_codex:
            if self.params.get("effort"):
                kwargs.setdefault("reasoning_effort", str(self.params["effort"]))
            if self.params.get("codex_version"):
                kwargs.setdefault("version", str(self.params["codex_version"]))
            if self.params.get("codex_login", True) and not agent.get("base_url"):
                env.setdefault("CODEX_FORCE_AUTH_JSON", "1")   # Harbor uploads ~/.codex/auth.json (ChatGPT login)
        if deployed is not None:
            if not self.deploys_adapter:
                raise ValueError(f"the acting agent {agent.get('import_path') or agent.get('name')} takes no adapter; "
                                 f"use {HERMIT_CODEX} (the default) or run with --adapter none")
            kwargs.update(deploy_dir=str(staging / "deploy"))
            if deployed.get("hooks"):
                kwargs["dangerously_bypass_hook_trust"] = True
        agent["kwargs"] = kwargs
        if env:
            agent["env"] = env
        return agent

    def environment_config(self) -> dict:
        env = {"delete": True, **(self.params.get("environment") or {})}
        return {k: v for k, v in env.items() if v is not None}

    def verifier_config(self) -> dict:
        return {k: v for k, v in (self.params.get("verifier") or {}).items() if v is not None}

    def job_config(self, task: dict, job_name: str, agent: dict) -> dict:
        """Harbor's job YAML for this one trial (a config file: `--agent` on the CLI discards the agents block)."""
        config = {"job_name": job_name, "jobs_dir": str(self.jobs_dir), "n_attempts": 1, "n_concurrent_trials": 1,
                  "tasks": [{"path": str(self.task_dir(task))}], "agents": [agent],
                  "environment": self.environment_config()}
        if self.verifier_config():
            config["verifier"] = self.verifier_config()
        if self.params.get("retry"):
            config["retry"] = dict(self.params["retry"])
        return config

    def harbor_env(self) -> dict:
        """The environment of Harbor's processes: the harborside package (and the example's agents) importable,
        a TMPDIR outside /tmp."""
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        paths = [str(HARBORSIDE_ROOT), *(str(self._path(p)) for p in (self.acting_agent.get("pythonpath") or []))]
        if os.environ.get("PYTHONPATH"):
            paths.append(os.environ["PYTHONPATH"])
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "TMPDIR": str(self.tmpdir)}
        if self.is_codex and self.params.get("codex_login", True):
            env.setdefault("CODEX_FORCE_AUTH_JSON", "1")
        return env

    def harbor_run(self, job: Path, config: Path, task: dict) -> tuple[int | None, bool]:
        """`harbor run` on the job config in its own process group; (exit code, expired). On expiry the group is
        killed and the trial's compose project taken down, best effort: Harbor would otherwise leave the container
        running after its client died."""
        with open(job / "harbor.log", "w") as log:
            proc = subprocess.Popen([self.harbor, "run", "-c", str(config), "-y", "-q"], env=self.harbor_env(),
                                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True, cwd=str(job))
            try:
                return proc.wait(timeout=self.wait_budget(task)), False
            except subprocess.TimeoutExpired:
                pass
            for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        for trial in self.trial_dirs(job, task):
            docker.compose_down(trial.name)
        return proc.returncode, True

    @staticmethod
    def trial_dirs(job: Path, task: dict) -> list[Path]:
        """Harbor's trial directories of the job: `<task>__<7-char id>` (one, for one trial)."""
        name = str(task.get("harbor_task") or task.get("task") or task["id"])
        return sorted(p for p in job.glob(f"{name}__*") if p.is_dir())

    # --- the halt sentinel and the disk gate ----------------------------------------------------------------
    def halt_file(self) -> Path:
        return self.jobs_dir / HALT_FILE

    def halted(self) -> str | None:
        try:
            return self.halt_file().read_text(encoding="utf-8")
        except OSError:
            return None

    def halt(self, verdict: dict, job: Path) -> None:
        """Record a halting verdict (usage limit, authentication): every later rollout returns at once."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.halt_file().write_text(f"{verdict['exception_type']}: {verdict['exception_message'][:2000]}\n"
                                    f"job: {job}\nwritten: {datetime.now().isoformat(timespec='seconds')}\n"
                                    f"Remove this file once the login or the usage window is restored.\n",
                                    encoding="utf-8")
        print(f"WARNING: {verdict['exception_type']} in {job.name}: nothing later in the run can succeed; every "
              f"rollout is halted while {self.halt_file()} exists", file=sys.stderr, flush=True)

    def wait_for_disk(self) -> bool:
        gate = self.params.get("disk_gate_gb")
        gate = float(DISK_GATE_GB if gate is None else gate)
        return docker.wait_for_disk(self._docker_root(), gate, float(self.params.get("disk_gate_wait_s") or DISK_GATE_WAIT_S))

    def _docker_root(self) -> Path:
        if getattr(self, "_root", None) is None:
            self._root = docker.data_root()
        return self._root

    def cleanup_images(self, task: dict) -> None:
        docker.cleanup_images(self.docker_cleanup, self.task_dir(task), self.jobs_dir / ".locks",
                              threshold_gb=float(self.params.get("rmi_threshold_gb") or 1.0))

    # --- the rollout ------------------------------------------------------------------------------------------
    def run(self, task: dict, adapter: Path | None) -> RunOutput:
        started = time.time()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        halted = self.halted()
        if halted is not None:                               # a usage-limit or login failure: no container
            return self._output(task, no_trial("halted", f"halted by {self.halt_file()}: "
                                                         f"{halted.splitlines()[0] if halted else ''}", halt=True),
                                None, None, started)
        if not self.task_dir(task).is_dir():
            raise ValueError(f"no Harbor task directory {self.task_dir(task)} for {task['id']}")
        if task.get("step") is not None:
            return self._run_step(task, adapter, started)
        return self._run_trial(task, adapter, started)

    def _run_trial(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        if not self.wait_for_disk():
            return self._output(task, no_trial("disk", f"the Docker data root {self._docker_root()} stayed under "
                                                       f"{self.params.get('disk_gate_gb', DISK_GATE_GB)} GB free: no "
                                                       "container started"), None, None, started)
        job_name = f"{_safe(task['id'])}-{uuid.uuid4().hex[:8]}"
        job = self.jobs_dir / job_name
        job.mkdir()
        deployed = stage(adapter, job, self.python, self.params.get("sandbox_enforce"))
        agent = self.agent_config(task, job if deployed is not None else None, deployed)
        (job / "job.yaml").write_text(yaml.safe_dump(self.job_config(task, job_name, agent), sort_keys=False))
        code, expired = self.harbor_run(job, job / "job.yaml", task)
        trials = [t for t in self.trial_dirs(job, task) if (t / "result.json").is_file()]
        trial = trials[-1] if trials else None
        result = None
        if trial is not None:
            try:
                result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
            except ValueError:
                result = None
        verdict = classify(result, expired=expired, harbor_exit=code, reward_key=self.reward_key)
        if verdict["halt"]:
            self.halt(verdict, job)
        log = curate_mod.curate(trial, job, verdict)
        self.cleanup_images(task)
        return self._output(task, verdict, result, log, started, trial=trial.name if trial else None,
                            extra={"harbor_job": str(job), "harbor_exit": code, "deployed": deployed},
                            trajectory=trial / "agent" / "trajectory.json" if trial else None)

    def _run_step(self, task: dict, adapter: Path | None, started: float) -> RunOutput:
        """One step of a multi-step task, through the stream's driver."""
        index, name = int(task["step_index"]), str(task["step"])
        total = int(task.get("steps") or 0)
        stream = _safe(f"{task.get('harbor_task') or task['id']}__s{task.get('rollout', 1)}")
        stream_dir = self.jobs_dir / "streams" / stream
        settings = {"idle_s": 4 * 3600, "start_timeout_s": 3600, **(self.params.get("stream") or {})}
        client = StreamClient(stream_dir, python=self.harbor_python)
        step_dir = stream_dir / "steps" / f"{index:03d}-{_safe(name)}"
        step_dir.mkdir(parents=True, exist_ok=True)
        deployed = stage(adapter, step_dir, self.python, self.params.get("sandbox_enforce"))
        agent = self.agent_config(task, step_dir if deployed is not None else None, deployed)
        (step_dir / "agent.json").write_text(json.dumps(agent, indent=1) + "\n")
        reply, result, error, cause = None, None, None, "stream_lost"
        try:
            if client.running() is None and not self.wait_for_disk():
                return self._output(task, no_trial("disk", f"the Docker data root {self._docker_root()} stayed under "
                                                           f"{self.params.get('disk_gate_gb', DISK_GATE_GB)} GB free: "
                                                           "the stream's environment was not started"), None, None, started)
            client.ensure(task_dir=self.task_dir(task), name=stream, agent=agent, environment=self.environment_config(),
                          verifier=self.verifier_config(), idle_s=float(settings["idle_s"]), env=self.harbor_env(),
                          start_timeout_s=float(settings["start_timeout_s"]))
            budget = self.wait_budget(task, build=False)
            # Earlier steps of the stream may be running in other rollouts of the same batch: wait for them, each
            # for its own budget at most (or `stream.turn_wait_s`).
            turn_wait = float(settings.get("turn_wait_s") or (max(1, index - 1) * budget + float(settings["start_timeout_s"])))
            client.wait_turn(index, timeout_s=turn_wait)
            reply = client.step(name=name, index=index, deploy_dir=agent["kwargs"].get("deploy_dir"),
                                agent_kwargs=agent["kwargs"], timeout_s=budget)
        except StreamOrderError as exc:
            error, cause = str(exc), "stream_order"
        except StreamError as exc:
            error = str(exc)
        if reply and reply.get("ok"):
            result = reply.get("step") or {}
            verdict = classify(result, reward_key=self.reward_key)
        elif reply and reply.get("expired"):
            verdict = classify(None, expired=True)
        else:
            if (reply or {}).get("out_of_order"):
                cause = "stream_order"
            verdict = no_trial(cause, error or (reply or {}).get("error") or json.dumps(reply)[:500])
        if verdict["halt"]:
            self.halt(verdict, step_dir)
        ran = bool(reply and reply.get("ok"))
        aborted = ran and bool(reply.get("aborted"))
        finished = ran and ((total and index >= total) or bool(reply.get("last")) or aborted)
        if finished or (reply or {}).get("expired") or verdict["halt"]:
            client.stop()                                    # the environment has no further use
            if finished:
                self.cleanup_images(task)
        outputs = Path(reply["paths"]["step_dir"]) if reply and reply.get("ok") and reply.get("paths") else None
        log = curate_mod.curate(outputs, step_dir, verdict, result=result,
                                extra_files={DRIVER_LOG: stream_dir / DRIVER_LOG})
        return self._output(task, verdict, result, log, started, trial=stream, step=name,
                            extra={"harbor_job": str(step_dir), "stream_dir": str(stream_dir), "deployed": deployed,
                                   "aborted": aborted},
                            trajectory=outputs / "agent" / "trajectory.json" if outputs else None)

    def _output(self, task: dict, verdict: dict, result: dict | None, log: Path | None, started: float, *,
                trial: str | None = None, step: str | None = None, extra: dict | None = None,
                trajectory: Path | None = None) -> RunOutput:
        agent_result = (result or {}).get("agent_result") or {}
        metadata = agent_result.get("metadata") or {}
        hooks_active = metadata.get("hooks_active")          # None: nothing was deployed
        ok = verdict["status"] == "ok"
        n_in, n_out = agent_result.get("n_input_tokens"), agent_result.get("n_output_tokens")
        tokens = int(n_in) + int(n_out) if ok and n_in is not None and n_out is not None else None
        wall = (curate_mod.seconds((result or {}).get("agent_execution"))
                or curate_mod.seconds({"started_at": (result or {}).get("started_at"),
                                       "finished_at": (result or {}).get("finished_at")})
                or round(time.time() - started, 1))
        status = verdict["status"]
        rewards = ((result or {}).get("verifier_result") or {}).get("rewards") if result else None
        output = {"harbor_task": task.get("harbor_task") or task.get("task") or task["id"], "rollout": task.get("rollout"),
                  "step": step, "reward": verdict["reward"], "rewards": rewards, "status": status,
                  "failure_cause": verdict["failure_cause"], "exception_type": verdict["exception_type"],
                  "trial": trial, "hooks_active": hooks_active}
        return RunOutput(output=output, log=log, cost_usd=agent_result.get("cost_usd"), wall_s=wall, tokens=tokens,
                         api_speed={"model_calls": curate_mod.model_calls(trajectory) if trajectory else 0,
                                    "status": status},
                         extra={"status": status, "timed_out": verdict["timed_out"],
                                "exception_type": verdict["exception_type"],
                                "exception_message": verdict["exception_message"][:2000],
                                "failure_cause": verdict["failure_cause"], "halt": verdict["halt"],
                                "n_cache_tokens": agent_result.get("n_cache_tokens"),
                                "verifier_reward_raw": verdict["reward"], "hooks_active": hooks_active,
                                "agent_metadata": metadata, **(extra or {})})
