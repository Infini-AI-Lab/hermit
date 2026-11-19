"""One task's Harbor environment, served to Hermit: run under Harbor's own interpreter, it imports harbor and the
standard library only (never hermit), and speaks JSON lines on its stdin and stdout.

    <harbor venv>/bin/python -I harbor_driver.py        # hermit.environment.harbor starts it, one per task

Harbor gives the task's data, its environment and its verifier; the agent is Hermit's. The driver builds Harbor's own
trial of the task (`Trial.create`, a single-step task) with Harbor's do-nothing agent (`nop`) in the agent's place and
drives its lifecycle a piece at a time, so that everything Harbor does to a task's environment is Harbor's own code:
the environment's start (the prebuilt image pulled, or the Dockerfile built, with the build timeout and its
multiplier; the healthcheck; the task's skills), the agent phase's network policy and user, the verifier (the task's
tests uploaded only now, `[verifier] environment_mode` shared or separate, its timeout and multiplier, the reward
files), the artifacts and the trial's result.json. Between the start and the verifier Hermit's agent works in the
environment through `exec`, `upload` and `download`.

Requests, one JSON object a line: {"id": n, "op": <op>, ...}; each is answered, in any order, by one line
{"id": n, "ok": true, ...} or {"id": n, "ok": false, "error": "<Type>: <message>", "type": "<Type>"}.

    start     {task_dir, trials_dir, trial_name, agent, environment, verifier, trial}
              -> {workdir, user, agent_timeout_sec, trial_dir, verifier_mode}: Harbor's TrialConfig parts (agent:
              override_timeout_sec, override_setup_timeout_sec, extra_allowed_hosts; environment and verifier as
              Harbor's EnvironmentConfig and VerifierConfig; trial: the timeout multipliers); the environment is up
    exec      {command, cwd, env, timeout_sec, stdin, user} -> {code, output, timed_out}: `command` (a shell line) in
              the main container, as the task's agent user unless `user` names another; with a timeout, under
              `timeout` there (the command's process group killed when it runs out), and `stdin` given through a file
    upload    {source, target}: a file or a folder of this machine to `target` (owned by the agent user, when the task
              names one)
    download  {source, target}: a file or a folder of the environment to `target` on this machine
    install   {agent, version} -> {version}: the CLI installed by Harbor's own installed agent of that name
    verify    {agent_logs} -> {rewards, exception, verifier_dir, verifier_mode}: the agent phase ends (its network
              policy and user restored), `agent_logs` (a folder of this machine) is copied to the trial's agent logs
              (/logs/agent, which a verifier may read), and Harbor's verifier runs: rewards as Harbor read them, or
              the exception it raised ({type, message}); the agent's environment is stopped as Harbor's trial stops it
    stop      -> {result}: the trial's result.json written and the environment down; the driver exits

A variable `exec` passes reaches a local Docker environment's `docker compose exec` by name (`-e NAME`, its value in
the compose client's own environment): never on a command line of this machine. The driver ends with the trial
stopped when its stdin closes (Hermit's process ended); a driver killed by a signal leaves the containers up, which
Hermit takes down by the trial's compose project names (hermit.environment.harbor_docker.compose_down).
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import traceback
import uuid

from harbor.agents.factory import AgentFactory
from harbor.models.agent.name import AgentName
from harbor.models.task.verifier_mode import VerifierEnvironmentMode, resolve_task_verifier_mode
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.models.trial.result import TimingInfo
from harbor.trial.trial import Trial

TRIAL_FIELDS = ("verifier_timeout_multiplier", "environment_build_timeout_multiplier", "agent_timeout_multiplier",
                "agent_setup_timeout_multiplier", "timeout_multiplier")
AGENT_FIELDS = ("override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec", "extra_allowed_hosts")
KILL_GRACE_S = 10                       # `timeout -k`: after the TERM, the KILL
BACKSTOP_S = 60                         # Harbor's own exec timeout past the command's, should `timeout` not end it


def log(message: str) -> None:
    try:
        print(f"[harbor-driver {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)
    except OSError:
        pass


class Driver:
    def __init__(self, out):
        self.out = out
        self.trial: Trial | None = None
        self.agent_phase: contextlib.AsyncExitStack | None = None
        self.user = None
        self.has_timeout: bool | None = None
        self.verified = False
        self.stopped = False
        self.lock = asyncio.Lock()                     # one request that changes the trial's phase at a time

    def reply(self, rid, payload: dict) -> None:
        self.out.write(json.dumps({"id": rid, **payload}, default=str) + "\n")
        self.out.flush()

    @property
    def env(self):
        if self.trial is None:
            raise RuntimeError("the environment is not started")
        return self.trial.agent_environment

    # --- the lifecycle ------------------------------------------------------------------------------------------
    async def start(self, req: dict) -> dict:
        if self.trial is not None:
            raise RuntimeError("the environment is already started")
        agent = {k: v for k, v in (req.get("agent") or {}).items() if k in AGENT_FIELDS and v is not None}
        fields = {k: v for k, v in (req.get("trial") or {}).items() if k in TRIAL_FIELDS and v is not None}
        config = TrialConfig(task=TaskConfig(path=Path(req["task_dir"])), trial_name=req["trial_name"],
                             trials_dir=Path(req["trials_dir"]), agent=AgentConfig(name=AgentName.NOP.value, **agent),
                             environment=EnvironmentConfig(**(req.get("environment") or {})),
                             verifier=VerifierConfig(**(req.get("verifier") or {})), job_id=uuid.uuid4(), **fields)
        trial = await Trial.create(config)
        if trial.task.has_steps:
            raise ValueError(f"{trial.task.name} has [[steps]]: a multi-step task is not served here")
        self.trial = trial
        trial._init_result()
        try:
            await trial._prepare()                      # the environment's start, Harbor's own (warm start)
        except Exception as exc:
            trial._record_exception(exc)                # in the trial's result.json, which stop writes
            raise
        self.user = trial.task.config.agent.user
        stack = contextlib.AsyncExitStack()
        plan = trial._network_plan(None, warn_unknown_inference=True)
        stack.enter_context(trial.agent_environment.with_default_user(self.user))
        await stack.enter_async_context(trial._phase_network_policy(
            trial.agent_environment, baseline_policy=plan.agent_env_baseline, phase_policy=plan.agent_phase))
        self.agent_phase = stack
        trial.result.agent_execution = TimingInfo(started_at=datetime.now(timezone.utc))
        found = await trial.agent_environment.exec("pwd")
        workdir = (found.stdout or "").strip().splitlines()[-1] if (found.stdout or "").strip() else "/"
        return {"workdir": workdir, "user": self.user, "agent_timeout_sec": trial.agent_timeout_sec,
                "trial_dir": str(trial.paths.trial_dir), "os": str(trial.task.config.environment.os.value),
                "verifier_mode": resolve_task_verifier_mode(trial.task.config).value}

    async def _end_agent_phase(self) -> None:
        if self.agent_phase is not None:
            stack, self.agent_phase = self.agent_phase, None
            await stack.aclose()
            execution = self.trial.result.agent_execution
            if execution is not None:
                execution.finished_at = datetime.now(timezone.utc)

    async def verify(self, req: dict) -> dict:
        trial = self.trial
        if trial is None:
            raise RuntimeError("the environment is not started")
        if self.verified:
            raise RuntimeError("the task was verified already")
        self.verified = True
        await self._end_agent_phase()
        logs = req.get("agent_logs")
        if logs and Path(logs).is_dir():
            shutil.copytree(logs, trial.paths.agent_dir, symlinks=True, dirs_exist_ok=True)
        mode = resolve_task_verifier_mode(trial.task.config)
        exception = None
        try:
            # SingleStepTrial._run after its agent: logs back to a non-mounted environment, the artifacts, the
            # verifier in the environment the task's mode names
            await trial._upload_agent_logs()
            await trial._collect_artifacts(stop_main_before_sidecars=(mode == VerifierEnvironmentMode.SEPARATE))
            if mode == VerifierEnvironmentMode.SEPARATE:
                await trial._stop_agent_environment()
            await trial._run_verifier()
        except Exception as exc:                         # recorded as Harbor's trial records it, and answered
            trial._record_exception(exc)
            exception = {"type": type(exc).__name__, "message": str(exc)[-4000:]}
        finally:
            if mode == VerifierEnvironmentMode.SHARED:
                await trial._stop_agent_environment()
        result = trial.result.verifier_result
        return {"rewards": dict(result.rewards) if result is not None and result.rewards else None,
                "exception": exception, "verifier_dir": str(trial.paths.verifier_dir), "verifier_mode": mode.value}

    async def stop(self, req: dict | None = None) -> dict:
        if self.stopped or self.trial is None:
            self.stopped = True
            return {"result": None}
        self.stopped = True
        trial = self.trial
        try:
            await self._end_agent_phase()
        except Exception as exc:                         # noqa: BLE001 - the environment still goes down
            log(f"the agent phase did not end cleanly: {type(exc).__name__}: {exc}")
        try:
            await trial._finalize()                      # the environment down, result.json written
        finally:
            trial._close_logger_handler()
        return {"result": str(trial.paths.result_path)}

    # --- the agent's operations ---------------------------------------------------------------------------------
    async def _timeout_ok(self) -> bool:
        if self.has_timeout is None:
            found = await self.env.exec("command -v timeout >/dev/null 2>&1 && echo yes", user="root")
            self.has_timeout = "yes" in (found.stdout or "")
        return self.has_timeout

    async def exec(self, req: dict) -> dict:
        env = self.env
        command = str(req["command"])
        timeout = req.get("timeout_sec")
        user = req.get("user", None)
        staged = None
        if req.get("stdin") is not None:
            staged = f"/tmp/hermit-stdin-{uuid.uuid4().hex}"
            with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
                f.write(req["stdin"])
                local = f.name
            try:
                await env.upload_file(local, staged)
            finally:
                os.unlink(local)
            await env.exec(f"chmod 644 {shlex.quote(staged)}", user="root")
            command = f"( {command} ) < {shlex.quote(staged)}"
        outer = None
        if timeout:
            timeout = max(1, int(float(timeout) + 0.999))
            if await self._timeout_ok():
                command = f"timeout -k {KILL_GRACE_S} {timeout} bash -c {shlex.quote(command)}"
                outer = timeout + KILL_GRACE_S + BACKSTOP_S
            else:
                outer = timeout
        started = time.time()
        try:
            result = await self._exec(command, cwd=req.get("cwd"), env=req.get("env") or {}, timeout=outer,
                                      user=user)
            code = result.return_code
            output = (result.stdout or "") + (result.stderr or "")
        except asyncio.TimeoutError:
            code, output = -1, ""
        finally:
            if staged is not None:
                with contextlib.suppress(Exception):
                    await env.exec(f"rm -f {shlex.quote(staged)}", user="root")
        timed_out = bool(timeout) and (code in (124, 137, -1) and time.time() - started >= timeout - 1)
        return {"code": -1 if timed_out else code, "output": output, "timed_out": timed_out}

    async def _exec(self, command: str, *, cwd, env: dict, timeout, user):
        environment = self.env
        try:
            from harbor.environments.docker.docker import DockerEnvironment
        except ImportError:                              # noqa: BLE001
            DockerEnvironment = ()
        if env and isinstance(environment, DockerEnvironment) and not environment._is_windows_container:
            # DockerEnvironment._compose_exec, with each variable passed by name: its value is in the compose
            # client's environment (os.environ, which _run_docker_compose_command hands it), not on its command line
            merged = environment._merge_env(env) or {}
            os.environ.update({str(k): str(v) for k, v in merged.items()})
            workdir = cwd or environment.task_env_config.workdir
            argv = ["exec", *(["-w", str(workdir)] if workdir else [])]
            for name in merged:
                argv += ["-e", str(name)]
            resolved = environment._resolve_user(user)
            if resolved is not None:
                argv += ["-u", str(resolved)]
            argv += ["main", *environment._platform.exec_shell_args(command)]
            return await environment._run_docker_compose_command(argv, check=False, timeout_sec=timeout,
                                                                 on_output=environment._output_callback())
        return await environment.exec(command, cwd=cwd, env=env or None, timeout_sec=timeout, user=user)

    async def _own(self, target: str) -> None:
        """What was uploaded, the agent user's (an upload lands as root's)."""
        if self.user not in (None, "root", 0, "0"):
            await self.env.exec(f"chown -R {shlex.quote(str(self.user))} {shlex.quote(target)}", user="root")

    async def upload(self, req: dict) -> dict:
        source, target = Path(req["source"]), str(req["target"])
        parent = str(Path(target).parent)
        await self.env.exec(f"mkdir -p {shlex.quote(parent)}", user="root")
        if source.is_dir() and not source.is_symlink():
            await self.env.upload_dir(source, target)
        else:
            await self.env.upload_file(source, target)
        await self._own(target)
        return {}

    async def download(self, req: dict) -> dict:
        source, target = str(req["source"]), Path(req["target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if await self.env.is_dir(source, user="root"):
            await self.env.download_dir(source, target)
        else:
            await self.env.download_file(source, target)
        return {}

    async def install(self, req: dict) -> dict:
        trial = self.trial
        logs = trial.paths.trial_dir / "install" / str(req["agent"])
        logs.mkdir(parents=True, exist_ok=True)
        agent = AgentFactory.create_agent_from_name(AgentName(str(req["agent"])), logs_dir=logs,
                                                    version=req.get("version"))
        await agent.setup(self.env)                     # Harbor's install of the CLI, as its trials run it
        return {"version": agent.version()}

    # --- the loop -----------------------------------------------------------------------------------------------
    async def handle(self, req: dict) -> None:
        rid, op = req.get("id"), req.get("op")
        try:
            if op in ("start", "verify", "stop", "install"):
                async with self.lock:
                    payload = await getattr(self, op)(req)
            elif op in ("exec", "upload", "download"):
                if self.verified or self.stopped:
                    raise RuntimeError("the agent's phase is over: the task was verified or stopped")
                payload = await getattr(self, op)(req)
            else:
                raise ValueError(f"unknown op {op!r}")
            self.reply(rid, {"ok": True, **payload})
        except Exception as exc:                         # noqa: BLE001 - the caller gets the error
            log(f"{op}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}")
            self.reply(rid, {"ok": False, "error": f"{type(exc).__name__}: {exc}"[-4000:], "type": type(exc).__name__})


async def serve(out) -> int:
    driver = Driver(out)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1 << 30)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks: set[asyncio.Task] = set()
    stopping = None
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        task = asyncio.create_task(driver.handle(req))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        if req.get("op") == "stop":
            stopping = task
            break
    if stopping is not None:
        await stopping
    else:                                               # Hermit went away: the trial is stopped all the same
        for task in list(tasks):
            task.cancel()
        with contextlib.suppress(Exception):
            await driver.stop()
    return 0


def main() -> int:
    # The protocol's own stream: a copy of the stdout this process was given. Whatever Harbor or a library prints
    # goes to stderr, never into the replies.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    try:
        return asyncio.run(serve(out))
    except Exception as exc:                            # noqa: BLE001 - the log says why the driver died
        log(f"fatal: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
