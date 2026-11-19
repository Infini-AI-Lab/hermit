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
WORKDIR_MARK = "hermit-workdir:"         # the line the working directory's probe prints it on (shell noise aside)
KILL_GRACE_S = 10                       # `timeout -k`: after the TERM, the KILL
BACKSTOP_S = 60                         # Harbor's own exec timeout past the command's, should `timeout` not end it
MARK = "HERMIT_AGENT_EXEC"              # each agent command's variable: what kills it, and whatever it started, when
                                        # its time runs out or the verifier starts while it still runs (not after it
                                        # ended: a server it left for the tests stays), with no `ps` needed
# Debian 11 (bullseye) images: the bullseye pools on deb.debian.org no longer serve the release's packages, so an
# install fails before the CLI starts. Point main and security at archive.debian.org, accept its expired Release files,
# refresh the lists, once per environment; other images are untouched (the Harbor runner's _repair_apt_sources).
APT_REPAIR = ("if grep -qs 'VERSION_CODENAME=bullseye' /etc/os-release && command -v apt-get >/dev/null 2>&1; "
              "then sed -i "
              "-e 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
              "-e 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
              "-e 's|http://deb.debian.org/debian |http://archive.debian.org/debian |g' /etc/apt/sources.list "
              "/etc/apt/sources.list.d/*.list 2>/dev/null; "
              "echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99hermit-archive; "
              "apt-get update; fi")


def kill_marked(mark: str) -> str:
    """A shell line (run as root) that kills every process whose environment holds `MARK=<mark...>` (an agent's
    commands) and every process below those, whatever environment it was started with (one that cleared its
    variables loses the mark, never its parent): found through /proc, no `ps` needed."""
    return ("roots=''; for p in /proc/[0-9]*; do "
            f"tr '\\0' '\\n' < $p/environ 2>/dev/null | grep -q '^{MARK}={mark}' && roots=\"$roots ${{p#/proc/}}\"; "
            "done; all=$roots; todo=$roots; while [ -n \"$todo\" ]; do next=''; for p in /proc/[0-9]*; do "
            "s=$(cat $p/stat 2>/dev/null) || continue; s=${s##*) }; set -- $s; for q in $todo; do "
            "[ \"$2\" = \"$q\" ] && next=\"$next ${p#/proc/}\"; done; done; all=\"$all $next\"; todo=$next; done; "
            "[ -n \"$all\" ] && kill -9 $all 2>/dev/null; "
            # confirmed: none of them left but as a zombie, within 5 s; else exit 1 (the caller's failure)
            "n=0; while [ $n -lt 25 ]; do left=; for q in $all; do "
            "grep -qs '^State:[[:space:]]*[^Z]' /proc/$q/status && left=1; done; [ -z \"$left\" ] && exit 0; "
            "sleep 0.2; n=$((n+1)); done; exit 1")


ENV_PREFIX = "HERMIT_ENV_"
NAME = __import__("re").compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def container_prologue(values: dict, by_fd: dict) -> tuple[str, dict]:
    """(the shell lines that set a command's variables in the container, the variables that carry their values
    there): each value travels under a name of this driver's (HERMIT_ENV_<n>), the lines export it under its own name
    (or, for a secret `by_fd` names, move it into a pipe on a descriptor of its own, its variable naming the
    descriptor, as hermit.environment.docker's by_fd) and unset the carrier, before the command is exec'd: no process
    that outlives the lines (`timeout`, the CLI) holds a by_fd secret in its environment."""
    lines, carried, fd = [], {}, 3
    for i, (name, value) in enumerate(values.items()):
        if not NAME.match(name):
            raise ValueError(f"not a variable name: {name!r}")
        carrier = f"{ENV_PREFIX}{i}"
        carried[carrier] = value
        if name in by_fd:
            if not NAME.match(str(by_fd[name])):
                raise ValueError(f"not a variable name: {by_fd[name]!r}")
            lines.append(f"exec {fd}<<HERMIT_FD\n${{{carrier}}}\nHERMIT_FD\n{by_fd[name]}={fd}; export {by_fd[name]}")
            fd += 1
        else:
            lines.append(f'export {name}="${{{carrier}}}"')
        lines.append(f"unset {carrier}")
    missing = set(by_fd) - set(values)
    if missing:
        raise ValueError(f"by_fd names variables the command is not given: {sorted(missing)}")
    return "".join(line + "\n" for line in lines), carried


def scrubbed(data: bytes, secrets) -> bytes:
    """`data` with each of `secrets` replaced, as Harbor's trial scrubs its files (text only: a binary file is left)."""
    if not secrets or b"\0" in data[:8192]:
        return data
    for secret in sorted(secrets, key=len, reverse=True):
        data = data.replace(secret.encode(), b"[REDACTED]")
    return data


def scrub_tree(root: Path, secrets) -> None:
    """Every regular file under `root` scrubbed of `secrets` (links never followed)."""
    if not secrets:
        return
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            clean = scrubbed(data, secrets)
            if clean != data:
                path.write_bytes(clean)
        except OSError as exc:
            log(f"scrub: {path}: {exc}")


def safe_copy(source: Path, target: Path, secrets=()) -> None:
    """`source`'s files under `target`, never through a link there, however the agent's environment changes the folder
    it shares with this machine (/logs/agent) meanwhile: every step goes through a directory descriptor opened without
    following a link (O_NOFOLLOW), an entry where a folder goes is moved aside for one of this process's own, and a
    file is written to a fresh name in its folder (O_EXCL) and renamed over the entry there, which replaces a link
    instead of writing through it. `target` itself is this machine's (the trial's folder, the mount's root)."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def aside(parent: int, name: str) -> None:            # an entry in the way, never followed: renamed away
        with contextlib.suppress(FileNotFoundError):
            os.rename(name, f".hermit-gone-{uuid.uuid4().hex}", src_dir_fd=parent, dst_dir_fd=parent)

    def folder_in(parent: int, name: str) -> int:
        try:
            return os.open(name, flags, dir_fd=parent)
        except OSError:                                  # a link, a file, nothing: a folder of this process's own
            aside(parent, name)
            os.mkdir(name, 0o755, dir_fd=parent)
            return os.open(name, flags, dir_fd=parent)   # replaced meanwhile: raised, nothing followed

    def put(parent: int, name: str, path: Path) -> None:
        temp = f".hermit-{uuid.uuid4().hex}"
        if path.is_symlink():                            # a link is copied as the link it is
            os.symlink(os.readlink(path), temp, dir_fd=parent)
        else:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            with os.fdopen(fd, "wb") as out, open(path, "rb") as given:
                if secrets:                              # the agent's credentials never reach the verifier's folder
                    out.write(scrubbed(given.read(), secrets))
                else:
                    shutil.copyfileobj(given, out)
        try:
            os.rename(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError:                                  # a folder in the way
            aside(parent, name)
            os.rename(temp, name, src_dir_fd=parent, dst_dir_fd=parent)

    target.mkdir(parents=True, exist_ok=True)
    root = os.open(str(target), flags)
    try:
        for folder, dirs, files in os.walk(source):
            rel = Path(folder).relative_to(source)
            fds = [root]
            try:
                for part in rel.parts:
                    fds.append(folder_in(fds[-1], part))
                for name in [*files, *(d for d in dirs if (Path(folder) / d).is_symlink())]:
                    put(fds[-1], name, Path(folder) / name)
            finally:
                for fd in fds[1:]:
                    os.close(fd)
    finally:
        os.close(root)


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
        self.secrets: set = set()                      # the credentials the agent's commands were given (scrubbed)
        self.has_timeout: bool | None = None
        self.verified = False
        self.stopped = False
        self.plan = None                               # the agent phase's network plan (an install runs under the
                                                       # environment's baseline, as Harbor's setup does)
        self.mark = uuid.uuid4().hex                   # this environment's agent commands' mark (MARK)
        self.agent_ops: set = set()                    # the agent's operations in flight (drained before verify)
        self.running: set = set()                      # the marks of the agent commands still running
        self.unkilled: set = set()                     # marks whose processes were not confirmed ended
        self.broken: str | None = None                 # the agent phase's network policy could not be restored
        self.agent_allowed = asyncio.Event()           # cleared while an install runs under the setup network
        self.agent_allowed.set()
        self.repaired = False
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
        plan = self.plan = trial._network_plan(None, warn_unknown_inference=True)
        stack.enter_context(trial.agent_environment.with_default_user(self.user))
        await stack.enter_async_context(trial._phase_network_policy(
            trial.agent_environment, baseline_policy=plan.agent_env_baseline, phase_policy=plan.agent_phase))
        self.agent_phase = stack
        trial.result.agent_execution = TimingInfo(started_at=datetime.now(timezone.utc))
        found = await trial.agent_environment.exec(f"printf '{WORKDIR_MARK}%s\\n' \"$PWD\"")
        marked = [line[len(WORKDIR_MARK):] for line in ((found.stdout or "") + (found.stderr or "")).splitlines()
                  if line.startswith(WORKDIR_MARK)]
        if found.return_code != 0 or not marked or not marked[-1].startswith("/"):
            raise RuntimeError(f"the environment's working directory could not be read: {found.stdout!r}"[:500])
        workdir = marked[-1]
        return {"workdir": workdir, "user": self.user, "agent_timeout_sec": trial.agent_timeout_sec,
                "setup_timeout_sec": trial.agent_setup_timeout_sec,
                "verifier_timeout_sec": trial.verifier_timeout_sec,
                "trial_dir": str(trial.paths.trial_dir), "os": str(trial.task.config.environment.os.value),
                "verifier_mode": resolve_task_verifier_mode(trial.task.config).value,
                "instruction": trial.task.instruction,    # Harbor's own, as its agents are given it
                "agent_network": str(getattr(plan.agent_phase.network_mode, "value", plan.agent_phase.network_mode))}

    async def _drain(self) -> None:
        """The agent's operations still in flight ended before the verifier starts: their requests cancelled, and the
        processes of each command still running killed in the environment (MARK), so that no command of the agent's
        runs on while the task's tests are there. What a command that already ended left running (a server the task
        asked for) stays, as Harbor's own agents leave it."""
        marks = [*self.running, *self.unkilled]
        for task in list(self.agent_ops):
            task.cancel()
        if self.agent_ops:
            await asyncio.gather(*self.agent_ops, return_exceptions=True)
        for mark in marks:                               # confirmed, or no verifier: the environment's failure
            await self._kill(mark)

    async def _kill(self, mark: str) -> None:
        """Every process of the agent command `mark` ended, confirmed (kill_marked's exit), from `/`; one not
        confirmed is kept (`unkilled`, tried again before the verifier) and raised."""
        try:
            done = await self.env.exec(kill_marked(mark), cwd="/", user="root")
            ok = done.return_code == 0
        except Exception as exc:                         # noqa: BLE001 - as unconfirmed
            ok, done = False, exc
        if not ok:
            self.unkilled.add(mark)
            raise RuntimeError(f"an agent command's processes could not be ended ({mark}): {str(done)[-300:]}")
        self.unkilled.discard(mark)

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
        self.verified = True                             # no agent operation starts from here on
        await self._drain()
        await self._end_agent_phase()
        logs = req.get("agent_logs")
        if logs and Path(logs).is_dir():
            safe_copy(Path(logs), trial.paths.agent_dir, self.secrets)
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
        if exception is not None:                        # scrubbed with the trial's files, before Hermit reads them
            said = trial.paths.trial_dir / "hermit-verify-exception.txt"
            said.write_text(exception["message"])
        self._scrub()
        if exception is not None:
            exception["message"] = said.read_text()
            said.unlink()
        return {"rewards": dict(result.rewards) if result is not None and result.rewards else None,
                "exception": exception, "verifier_dir": str(trial.paths.verifier_dir), "verifier_mode": mode.value}

    def _scrub(self) -> None:
        """Harbor's scrub of the trial's files (a credential the verifier's env names), as its trial runs it, and of the
        credentials the agent's commands were given (`secrets`), which Harbor's does not know."""
        try:
            self.trial._scrub_jobs_dir()
        except Exception as exc:                         # noqa: BLE001 - logged; the files stay as they are
            log(f"scrub: {type(exc).__name__}: {exc}")
        scrub_tree(self.trial.paths.trial_dir, self.secrets)

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
            self._scrub()
            trial._close_logger_handler()
        return {"result": str(trial.paths.result_path)}

    # --- the agent's operations ---------------------------------------------------------------------------------
    async def _timeout_ok(self) -> bool:
        if self.has_timeout is None:
            found = await self.env.exec("command -v timeout >/dev/null 2>&1 && echo yes", user="root")
            self.has_timeout = "yes" in (found.stdout or "")
        return self.has_timeout

    def _note_secrets(self, req: dict) -> None:
        """The credentials an agent command is given, kept to scrub: a value of a variable Harbor names as a secret,
        or one passed on a descriptor, as Harbor's own scrub reads its agents' env."""
        from harbor.utils.env import is_bool_env_value, is_sensitive_env_key
        by_fd = req.get("by_fd") or {}
        for key, value in (req.get("env") or {}).items():
            value = str(value or "")
            if value and (key in by_fd or is_sensitive_env_key(str(key))) and not is_bool_env_value(value):
                self.secrets.add(value)

    async def exec(self, req: dict) -> dict:
        self._note_secrets(req)
        command = str(req["command"])
        timeout = req.get("timeout_sec")
        user = req.get("user", None)
        mark = f"{self.mark}-{uuid.uuid4().hex[:8]}"
        while True:                     # admitted and counted at once: an install waits for every admitted command
            await self.agent_allowed.wait()
            if self.broken:
                raise RuntimeError(f"the environment is broken: {self.broken}")
            if self.agent_allowed.is_set():
                self.running.add(mark)
                break
        try:
            return await self._run_command(req, command, timeout, user, mark)
        finally:
            self.running.discard(mark)

    async def _run_command(self, req: dict, command: str, timeout, user, mark: str) -> dict:
        env = self.env
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
                command = f"bash -c {shlex.quote(command)}"
        else:
            command = f"bash -c {shlex.quote(command)}"
        started = time.time()
        try:
            result = await self._exec(command, cwd=req.get("cwd"), env={**(req.get("env") or {}), MARK: mark},
                                      by_fd=req.get("by_fd") or {}, timeout=outer, user=user)
            code = result.return_code
            output = (result.stdout or "") + (result.stderr or "")
        except Exception as exc:                        # Harbor's own client timeout (no `timeout` in the image)
            if not (timeout and time.time() - started >= timeout - 1):
                raise
            code, output = -1, f"{type(exc).__name__}: {exc}"
        finally:
            if staged is not None:
                with contextlib.suppress(Exception):
                    await env.exec(f"rm -f {shlex.quote(staged)}", user="root")
        timed_out = bool(timeout) and (code in (124, 137, -1) and time.time() - started >= timeout - 1)
        if timed_out:                                    # nothing it started outlives its time, `timeout` or not
            with contextlib.suppress(Exception):         # unconfirmed: kept, and ended before the verifier
                await self._kill(mark)
        return {"code": -1 if timed_out else code, "output": output, "timed_out": timed_out}

    async def _exec(self, command: str, *, cwd, env: dict, by_fd: dict, timeout, user):
        """`command` (a shell line that execs the command: `timeout ...` or `bash -c ...`) in the main container,
        its variables delivered there and nowhere on this machine (container_prologue): no variable the request names
        touches this process's environment or the compose client's own (PATH, DOCKER_HOST stay this machine's), and
        none is on a command line."""
        environment = self.env
        try:
            from harbor.environments.docker.docker import (DockerEnvironment,
                                                           _sanitize_docker_compose_project_name)
        except ImportError:                              # noqa: BLE001
            DockerEnvironment, _sanitize_docker_compose_project_name = (), None
        values = {str(k): str(v) for k, v in ((environment._merge_env(env) or {}) if env else {}).items()}
        prologue, carried = container_prologue(values, by_fd)
        line = prologue + "exec " + command
        if isinstance(environment, DockerEnvironment) and not environment._is_windows_container:
            # DockerEnvironment._compose_exec and _run_docker_compose_command, with the request's variables under
            # names of this driver's own (HERMIT_ENV_<n>), passed by name to `docker compose exec` from a private
            # copy of the client's environment
            workdir = cwd or environment.task_env_config.workdir
            argv = ["exec", *(["-w", str(workdir)] if workdir else [])]
            for name in carried:
                argv += ["-e", name]
            resolved = environment._resolve_user(user)
            if resolved is not None:
                argv += ["-u", str(resolved)]
            argv += ["main", *environment._platform.exec_shell_args(line)]
            runtime = type(environment).runtime()
            full = [*runtime.compose, "--project-name", _sanitize_docker_compose_project_name(environment.session_id)]
            if runtime.supports_compose_project_directory:
                full += ["--project-directory", str(environment.environment_dir.resolve().absolute())]
            for path in environment._docker_compose_paths:
                full += ["-f", str(path.resolve().absolute())]
            client = {**environment._compose_env_vars(include_os_env=True), **carried}
            process = await asyncio.create_subprocess_exec(
                *full, *argv, env=client, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(environment.environment_dir.resolve().absolute()) if environment.environment_dir.is_dir()
                else None)
            return await DockerEnvironment._collect_buffered_output(process, timeout_sec=timeout)
        return await environment.exec(line, cwd=cwd, env=carried or None, timeout_sec=timeout, user=user)

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

    async def _repair(self) -> None:
        if not self.repaired:
            self.repaired = True
            with contextlib.suppress(Exception):         # a failure here is left to the install to report
                await self.env.exec(APT_REPAIR, user="root")

    @contextlib.asynccontextmanager
    async def _setup_phase(self):
        """An install as Harbor's setup runs one: under the environment's baseline network policy, not the agent
        phase's (a task whose agent may not reach the network still installs its CLI), then back."""
        plan, env = self.plan, self.env
        switch = plan is not None and self.agent_phase is not None and plan.agent_phase != plan.agent_env_baseline
        if not switch:
            yield
            return
        self.agent_allowed.clear()     # no agent command runs under the wider policy: new ones wait, running ones end
        try:
            while self.running:
                await asyncio.sleep(0.2)
            await env.set_network_policy(plan.agent_env_baseline)
            try:
                yield
            finally:
                try:
                    await env.set_network_policy(plan.agent_phase)
                except BaseException as exc:
                    # never back to agent commands under the setup's wider policy: the environment is done for
                    self.broken = f"the agent phase's network policy could not be restored: {exc}"
                    raise
        finally:
            self.agent_allowed.set()

    def _installer(self, name: str, version=None):
        logs = self.trial.paths.trial_dir / "install" / name
        logs.mkdir(parents=True, exist_ok=True)
        return AgentFactory.create_agent_from_name(AgentName(name), logs_dir=logs, version=version)

    async def install(self, req: dict) -> dict:
        if self.broken:
            raise RuntimeError(f"the environment is broken: {self.broken}")
        trial = self.trial
        agent = self._installer(str(req["agent"]), req.get("version"))
        async with self._setup_phase():
            await self._repair()
            # Harbor's install of the CLI, as its trials run it: within Harbor's agent setup timeout
            await asyncio.wait_for(agent.setup(self.env), timeout=trial.agent_setup_timeout_sec)
        return {"version": agent.version()}

    async def ensure(self, req: dict) -> dict:
        """System packages (python3 for an adapter's hooks) with Harbor's own installer, as the runner ensured them."""
        if self.broken:
            raise RuntimeError(f"the environment is broken: {self.broken}")
        agent = self._installer("codex")
        async with self._setup_phase():
            await self._repair()
            await asyncio.wait_for(agent.ensure_system_dependencies(self.env, tuple(req.get("packages") or ())),
                                   timeout=self.trial.agent_setup_timeout_sec)
        return {}

    # --- the loop -----------------------------------------------------------------------------------------------
    async def handle(self, req: dict) -> None:
        rid, op = req.get("id"), req.get("op")
        try:
            if op in ("start", "verify", "stop", "install", "ensure"):
                async with self.lock:
                    payload = await getattr(self, op)(req)
            elif op in ("exec", "upload", "download"):
                if self.verified or self.stopped:
                    raise RuntimeError("the agent's phase is over: the task was verified or stopped")
                if self.broken:
                    raise RuntimeError(f"the environment is broken: {self.broken}")
                task = asyncio.current_task()
                self.agent_ops.add(task)
                try:
                    payload = await getattr(self, op)(req)
                except asyncio.CancelledError:          # drained: the verifier starts
                    self.reply(rid, {"ok": False, "error": f"{op}: the agent's phase ended (the verifier started)",
                                     "type": "AgentPhaseOver"})
                    return
                finally:
                    self.agent_ops.discard(task)
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
