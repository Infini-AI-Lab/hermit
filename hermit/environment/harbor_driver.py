"""One task's Harbor trial, served to Hermit: run under Harbor's own interpreter, it imports harbor and the standard
library only (never hermit), and speaks JSON lines on its stdin and stdout.

    <harbor venv>/bin/python -I harbor_driver.py        # hermit.environment.harbor starts it, one per task

Harbor runs the whole trial with its own `Trial.run()`: the environment's start (the prebuilt image pulled or the
Dockerfile built, its build timeout and multiplier, the healthcheck), Harbor's capability check (a task's skills or
MCP servers, a Windows container: refused for this bridge),
the agent's setup (under Harbor's setup timeout, the baseline network policy, the agent user), the agent phase (the
task's agent network policy, its user, its timeout), the verifier (`[verifier] environment_mode` shared or separate,
the tests uploaded only now, its timeout), the scrub of the trial's files, result.json and the environment's stop.
The agent in it is HermitBridge, a Harbor agent with no logic of its own (an external agent, as Harbor calls one):
its setup installs what Hermit asked for with Harbor's own installers, and its run relays Hermit's agent commands to
the environment until Hermit says it is done, or Harbor's agent timeout or a cancellation ends it; either way it ends
every command it started that still runs before it returns (what a command that ended left running, a server the
task asked for, stays, as the runner's agents left it). Harbor then verifies.

Requests, one JSON object a line: {"id": n, "op": <op>, ...}; each is answered, in any order, by one line
{"id": n, "ok": true, ...} or {"id": n, "ok": false, "error": "<Type>: <message>", "type": "<Type>"}.

    start     {task_dir, trials_dir, trial_name, agent, environment, verifier, trial, installs}
              -> {workdir, user, agent_timeout_sec, setup_timeout_sec, verifier_timeout_sec, trial_dir, instruction,
              agent_network, installed, verifier_mode}: answered once the agent phase began (the environment up, the
              installs done); a trial that ended before it is answered with its exception (its type: Harbor's, or
              UnsupportedTask: a task Harbor runs for none of Hermit's agents)
    exec      {command, cwd, env, timeout_sec, stdin, user, by_fd} -> {code, output, timed_out}: `command` (a shell
              line) in the main container, as the task's agent user unless `user` names another; with a timeout, under
              `timeout` there, and `stdin` given through a file; only during the agent phase
    upload    {source, target, as_agent}: a file or a folder of this machine to `target`, given to the agent user
              when it is not root (Hermit's provisioning); `as_agent` (the agent's process asked, through the
              sandbox's proxy): written with that user's own permissions; download {source, target, as_agent}: the
              other way, `as_agent` read with that user's permissions
    done      {agent_logs} -> {rewards, exception, verifier_dir, verifier_mode, trial_dir}: the agent phase ends (its
              commands still running ended, `agent_logs`, a folder of this machine, copied scrubbed into the trial's
              agent logs, /logs/agent, which a verifier may read), and the trial runs on to its end
    stop      -> {result}: a trial still running is cancelled (Harbor's own teardown); the driver exits

A variable `exec` passes reaches a local Docker environment's `docker compose exec` by name (`-e NAME`, its value in
the compose client's own environment): never on a command line of this machine. The driver ends with the trial
stopped when its stdin closes (Hermit's process ended); a driver killed by a signal leaves the containers up, which
Hermit takes down by the trial's compose project names (hermit.environment.harbor_docker.compose_down).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
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

from harbor.agents.base import BaseAgent
from harbor.agents.factory import AgentFactory
from harbor.models.agent.name import AgentName
from harbor.models.task.verifier_mode import resolve_task_verifier_mode
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.trial.trial import Trial

MODULE = "hermit_harbor_driver"         # this module's name for Harbor's agent import (AgentConfig.import_path)
BRIDGE = f"{MODULE}:HermitBridge"
UNSUPPORTED = ("does not support", "cannot forward HTTP MCP")   # Harbor's capability refusals of an agent
VERSION_BEGIN, VERSION_END = "hermit-version-begin", "hermit-version-end"   # around a mounted binary's --version
# Where TLS clients find the system's CA bundle (the runner's harborside.codex)
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
              "/etc/pki/tls/cacert.pem", "/etc/ssl/cert.pem")


def host_ca_bundle() -> Path | None:
    """The CA bundle OpenSSL trusts on this machine, else certifi's; None when there is neither."""
    import ssl
    paths = ssl.get_default_verify_paths()
    for name in (os.environ.get(paths.openssl_cafile_env or ""), paths.cafile, paths.openssl_cafile):
        if name and Path(name).is_file():
            return Path(name).resolve()
    try:
        import certifi
    except ImportError:
        return None
    return Path(certifi.where())

TRIAL_FIELDS = ("verifier_timeout_multiplier", "environment_build_timeout_multiplier", "agent_timeout_multiplier",
                "agent_setup_timeout_multiplier", "timeout_multiplier")
AGENT_FIELDS = ("override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec", "extra_allowed_hosts")
OWNER_MARK = "hermit-owner:"             # the agent user's uid:gid line (the runner's OWNER_LINE)
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
            "st=$(sed -n 's/^State:[[:space:]]*\\([A-Za-z]\\).*/\\1/p' /proc/$q/status 2>/dev/null); "
            "[ -n \"$st\" ] && [ \"$st\" != Z ] && left=1; done; [ -z \"$left\" ] && exit 0; "
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



# Every Docker client this process starts (Harbor's and its own: asyncio.create_subprocess_exec), with the operation it
# serves: Harbor's collector leaves a client running when its awaiting task is cancelled, so the bridge ends the
# clients of the agent's commands before it returns, and the driver every client before a teardown on Hermit's exit.
CLIENTS: dict = {}                       # process -> "agent" or "lifecycle"
CLIENT_KIND: contextvars.ContextVar = contextvars.ContextVar("hermit_client_kind", default="lifecycle")
_spawn = asyncio.create_subprocess_exec


async def _tracked_spawn(*args, **kwargs):
    process = await _spawn(*args, **kwargs)
    for known in [p for p in CLIENTS if p.returncode is not None]:
        CLIENTS.pop(known, None)
    CLIENTS[process] = CLIENT_KIND.get()
    return process

asyncio.create_subprocess_exec = _tracked_spawn
_communicate = asyncio.subprocess.Process.communicate


async def _communicate_or_kill(self, input=None):
    """Process.communicate, the client killed when the wait for it is cancelled (Harbor's collector leaves it
    running): a compose up its build timeout gave up on, a transfer or an exec the phase's end cancelled, never left
    to finish after the teardown or the verifier began."""
    try:
        return await _communicate(self, input)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            self.kill()
        raise

asyncio.subprocess.Process.communicate = _communicate_or_kill
END_PHASE_S = 300                       # the end of the agent phase's own bound: transfers to finish, kills, removals


async def end_clients(kind: str | None = None) -> None:
    """The clients of `kind` (every one: None) still running killed and waited for."""
    for process, of in list(CLIENTS.items()):
        if process.returncode is None and (kind is None or of == kind):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 30)
        if process.returncode is not None:
            CLIENTS.pop(process, None)


class HarborDriverError(RuntimeError):
    """The bridge's own failure in the trial (a command it could not confirm ended, the working directory unread): a
    trial Harbor then records with this exception and does not verify, which Hermit reads as infra (harbor_failed)."""


class Failed(RuntimeError):
    """A request's failure with the type its reply names (`kind`)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


DRIVER: "Driver | None" = None          # the process's one driver, which the bridge reaches


class HermitBridge(BaseAgent):
    """Harbor's agent for a Hermit agent: no logic of its own (module docstring). Its capabilities are Harbor's
    defaults, so Harbor itself refuses a Windows task, a task's skills or MCP servers for it."""

    @staticmethod
    def name() -> str:
        return "hermit"

    def version(self) -> str | None:
        return None

    async def setup(self, environment) -> None:
        await DRIVER.setup(environment)

    async def run(self, instruction: str, environment, context) -> None:
        await DRIVER.phase(instruction, environment)


class Driver:
    def __init__(self, out):
        self.out = out
        self.trial: Trial | None = None
        self.run_task: asyncio.Task | None = None
        self.installs: list = []
        self.installed: dict = {}
        self.bins: dict = {}                           # cli -> whether its mounted binary serves (setup's word)
        self.phase_started: asyncio.Future = asyncio.get_running_loop().create_future()
        self.done = asyncio.Event()
        self.agent_logs: str | None = None
        self.env = None                                # the environment, while the agent phase is on
        self.in_phase = False
        self.user = None
        self.owner: str | None = None                  # the agent user's uid:gid when not root (the runner's _owner)
        self.has_timeout: bool | None = None
        self.mark = uuid.uuid4().hex                   # this environment's agent commands' mark (MARK)
        self.agent_ops: dict = {}                      # the agent's operations in flight: task -> op
        self.running: set = set()                      # the marks of the agent commands still running
        self.unkilled: set = set()                     # marks whose processes were not confirmed ended
        self.secrets: set = set()                      # the credentials the agent's commands were given (scrubbed)
        self.secret_files: set = set()                 # files there holding a credential: removed at the phase's end
        self.streams: dict = {}                        # mark -> (file there, log name): a running command's output
        self.exceptions: list = []                     # every exception the trial recorded or skipped, in order
        self.repaired = False
        self.lock = asyncio.Lock()                     # one request that changes the trial's phase at a time

    def reply(self, rid, payload: dict) -> None:
        self.out.write(json.dumps({"id": rid, **payload}, default=str) + "\n")
        self.out.flush()

    # --- the trial ------------------------------------------------------------------------------------------------
    async def start(self, req: dict) -> dict:
        if self.trial is not None:
            raise RuntimeError("the trial is already started")
        agent = {k: v for k, v in (req.get("agent") or {}).items() if k in AGENT_FIELDS and v is not None}
        fields = {k: v for k, v in (req.get("trial") or {}).items() if k in TRIAL_FIELDS and v is not None}
        config = TrialConfig(task=TaskConfig(path=Path(req["task_dir"])), trial_name=req["trial_name"],
                             trials_dir=Path(req["trials_dir"]), agent=AgentConfig(import_path=BRIDGE, **agent),
                             environment=EnvironmentConfig(**(req.get("environment") or {})),
                             verifier=VerifierConfig(**(req.get("verifier") or {})), job_id=uuid.uuid4(), **fields)
        try:
            trial = await Trial.create(config)
        except Exception as exc:                         # Harbor's refusal of the task before any trial
            if "windows" in str(exc).lower() and "support" in str(exc).lower():
                raise Failed("UnsupportedTask", str(exc)) from None
            raise
        recorded = trial._record_exception

        def record(exc, _recorded=recorded):             # Harbor keeps the first; the driver keeps them all
            self.exceptions.append(exc)
            _recorded(exc)
        trial._record_exception = record
        if trial.task.has_steps:
            raise ValueError(f"{trial.task.name} has [[steps]]: a multi-step task is not served here")
        self.trial, self.installs = trial, list(req.get("installs") or [])
        self.run_task = asyncio.create_task(trial.run())
        await asyncio.wait({self.run_task, self.phase_started}, return_when=asyncio.FIRST_COMPLETED)
        if self.phase_started.done() and not self.phase_started.cancelled():
            return self.phase_started.result()
        with contextlib.suppress(BaseException):        # the trial ended before its agent phase: its exception
            self.run_task.result()
        info = trial.result.exception_info
        kind = getattr(info, "exception_type", None) or "TrialEnded"
        message = getattr(info, "exception_message", None) or "the trial ended before its agent phase"
        if any(word in message for word in UNSUPPORTED):
            kind = "UnsupportedTask"
        raise Failed(kind, message)

    async def setup(self, environment) -> None:
        """Harbor's agent setup (its timeout, the baseline network policy, the agent user): each CLI Hermit asked
        for, by Harbor's own installed agent of that name (unless its probe finds it there and no version is
        pinned), and the system packages asked for, by Harbor's own installer."""
        for item in self.installs:
            if item.get("bin"):                          # a binary of this machine the environment mounts
                served = await self._use_mounted(environment, item)
                self.bins[str(item["agent"])] = served
                if served:
                    continue
            elif item.get("probe") and not item.get("version"):
                found = await environment.exec(str(item["probe"]))
                if found.return_code == 0 and (found.stdout or "").strip():
                    continue
            if not self.repaired:
                self.repaired = True
                with contextlib.suppress(Exception):     # a failure here is left to the install to report
                    await environment.exec(APT_REPAIR, user="root")
            if item.get("agent"):
                agent = self._installer(str(item["agent"]), item.get("version"))
                await agent.setup(environment)
                self.installed[str(item["agent"])] = agent.version()
            if item.get("packages"):                     # best effort, as the runner's: the hooks may fail open
                try:
                    await self._installer("codex").ensure_system_dependencies(environment, tuple(item["packages"]))
                    for package in item["packages"]:
                        self.installed[str(package)] = True
                except Exception as exc:                 # noqa: BLE001 - the session runs; its hooks are warned
                    log(f"setup: {', '.join(item['packages'])} could not be installed: {type(exc).__name__}: {exc}")

    async def _use_mounted(self, environment, item: dict) -> bool:
        """The mounted binary, when it runs there as the asked-for version (any, when none is asked for) and the user
        the session runs as may execute it (the runner's _use_mounted): linked onto PATH, and for Codex the release's
        rg when the image has none and a CA bundle when the image has none (its _after_mount); else False, and the
        CLI is installed instead (the session then runs the installed one)."""
        binary, version = str(item["bin"]), item.get("version")
        q = shlex.quote
        got = await environment.exec(f"echo {VERSION_BEGIN}; {q(binary)} --version 2>&1; c=$?; "
                                     f"printf '\\n{VERSION_END}\\n'; exit \"$c\"", user="root")
        text = got.stdout or ""
        said = text.split(VERSION_BEGIN, 1)[-1].split(VERSION_END, 1)[0].strip() if VERSION_BEGIN in text else ""
        found = self._installer(str(item["agent"])).parse_version(said) if got.return_code == 0 and said else None
        if not found or (version and found != version):
            log(f"setup: the mounted {binary} does not run here as {version or 'any version'} ({said[:200]!r}): "
                "installing it")
            return False
        if (await environment.exec(f"test -x {q(binary)}")).return_code != 0:
            log(f"setup: the mounted {binary} may not be executed here by the session's user: installing it")
            return False
        program = str(item.get("program") or Path(binary).name)
        await environment.exec(f"mkdir -p /usr/local/bin && ln -sf {q(binary)} /usr/local/bin/{q(program)}",
                               user="root")
        if item["agent"] == "codex":
            rg = q(str(Path(binary).parent.parent / "codex-path" / "rg"))
            await environment.exec(f"if ! command -v rg >/dev/null 2>&1 && [ -x {rg} ]; then mkdir -p /usr/local/bin "
                                   f"&& ln -sf {rg} /usr/local/bin/rg; fi", user="root")
            await self._ensure_ca_bundle(environment)
        self.installed[str(item["agent"])] = found
        return True

    async def _ensure_ca_bundle(self, environment) -> None:
        """TLS roots for a mounted Codex: the image's own bundle, else this machine's copied to the Debian path (the
        runner's: without them every request fails and the session serves nothing)."""
        check = " || ".join(f"[ -s {path} ]" for path in CA_BUNDLES)
        if (await environment.exec(check, user="root")).return_code == 0:
            return
        source = host_ca_bundle()
        if source is None:
            log("setup: the image has no CA bundle and neither has this machine: Codex may not reach its API")
            return
        target = CA_BUNDLES[0]
        await environment.exec(f"mkdir -p {shlex.quote(str(Path(target).parent))}", user="root")
        await environment.upload_file(source, target)
        await environment.exec(f"chmod 644 {shlex.quote(target)}", user="root")

    def _installer(self, name: str, version=None):
        logs = self.trial.paths.trial_dir / "install" / name
        logs.mkdir(parents=True, exist_ok=True)
        return AgentFactory.create_agent_from_name(AgentName(name), logs_dir=logs, version=version)

    async def phase(self, instruction: str, environment) -> None:
        """The bridge's run: the agent phase, Hermit's commands relayed until `done`, Harbor's timeout or a
        cancellation; the commands still running ended (confirmed: one that is not fails the trial before any
        verifier) and Hermit's logs copied, scrubbed, into the trial's agent logs."""
        trial = self.trial
        self.env, self.user = environment, trial.task.config.agent.user
        found = await environment.exec(f"printf '{WORKDIR_MARK}%s\\n' \"$PWD\"")
        marked = [line[len(WORKDIR_MARK):] for line in ((found.stdout or "") + (found.stderr or "")).splitlines()
                  if line.startswith(WORKDIR_MARK)]
        if found.return_code != 0 or not marked or not marked[-1].startswith("/"):
            raise HarborDriverError(f"the environment's working directory could not be read: {found.stdout!r}"[:500])
        said = await environment.exec(f'echo "{OWNER_MARK}$(id -u):$(id -g)"')   # the user the agent runs as
        owner = [line[len(OWNER_MARK):].strip() for line in ((said.stdout or "") + (said.stderr or "")).splitlines()
                 if line.startswith(OWNER_MARK)]
        if said.return_code != 0 or not owner or not __import__("re").fullmatch(r"\d+:\d+", owner[-1]):
            raise HarborDriverError(f"the task's agent user has no uid:gid here: {said.stdout!r}"[:500])
        self.owner = None if owner[-1].split(":")[0] == "0" else owner[-1]
        plan = trial._network_plan(None)
        self.in_phase = True
        self.phase_started.set_result({
            "workdir": marked[-1], "user": self.user, "agent_timeout_sec": trial.agent_timeout_sec,
            "setup_timeout_sec": trial.agent_setup_timeout_sec, "verifier_timeout_sec": trial.verifier_timeout_sec,
            "trial_dir": str(trial.paths.trial_dir), "os": str(trial.task.config.environment.os.value),
            "verifier_mode": resolve_task_verifier_mode(trial.task.config).value, "instruction": instruction,
            "agent_network": str(getattr(plan.agent_phase.network_mode, "value", plan.agent_phase.network_mode)),
            "installed": self.installed, "bins": self.bins})
        cancelled = False
        try:
            await self.done.wait()
        except asyncio.CancelledError:                   # Harbor's agent timeout, or the trial cancelled
            cancelled = True
        self.in_phase = False
        # The phase's end, a task of its own awaited through any cancellation (Harbor's timeout cancels this run, and
        # waits for it): it is never cut short by the deadline it follows, and is bounded by its own END_PHASE_S
        ending = asyncio.ensure_future(asyncio.wait_for(self._end_phase(), END_PHASE_S))
        while not ending.done():
            try:
                await asyncio.shield(ending)
            except asyncio.CancelledError:
                cancelled = True
        try:
            ending.result()
        except asyncio.TimeoutError:
            raise HarborDriverError(f"the agent phase's end did not finish in {END_PHASE_S} s") from None
        if cancelled:
            raise asyncio.CancelledError()

    async def _end_phase(self) -> None:
        """What the end of the agent phase does before Harbor verifies, however it ended: transfers in flight let
        finish (their files whole), the commands still running ended (their Docker clients, and their processes there,
        MARK: those of the running ones and of any earlier kill not confirmed, confirmed), a running command's output
        kept in the agent logs under its name (a stream the verifier may read), the credential files removed from the
        environment (confirmed), and Hermit's logs copied, scrubbed, into the trial's agent logs. A command that already
        ended left what it started (a server the task asked for), as Harbor's own agents leave it. Anything not
        confirmed raises HarborDriverError: Harbor records it and runs no verifier."""
        trial = self.trial
        transfers = [t for t, op in self.agent_ops.items() if op != "exec"]
        if transfers:
            await asyncio.wait(transfers, timeout=END_PHASE_S / 2)
        marks = sorted({*self.running, *self.unkilled})  # before the cancelled commands forget theirs
        for task in list(self.agent_ops):
            task.cancel()
        if self.agent_ops:
            await asyncio.gather(*self.agent_ops, return_exceptions=True)
        await end_clients("agent")
        for mark in marks:
            await self._kill(mark)
        if marks:                                        # one a client had dispatched as it was ended
            await asyncio.sleep(1)
            for mark in marks:
                await self._kill(mark)
        for mark, (path, name) in list(self.streams.items()):
            got = await self.env.exec(f"cat {shlex.quote(path)} 2>/dev/null; rm -f {shlex.quote(path)}", user="root")
            if got.stdout:
                trial.paths.agent_dir.mkdir(parents=True, exist_ok=True)
                kept = Path(self.out_dir()) / Path(name).name
                kept.write_bytes(scrubbed(got.stdout.encode(), self.secrets))
                safe_copy(kept.parent, trial.paths.agent_dir)
            self.streams.pop(mark, None)
        if self.secret_files:
            listed = " ".join(shlex.quote(f) for f in sorted(self.secret_files))
            gone = await self.env.exec(f"rm -f -- {listed}; for f in {listed}; do [ -e \"$f\" ] && exit 1; done; "
                                       "exit 0", cwd="/", user="root")
            if gone.return_code != 0:
                raise HarborDriverError("a credential file could not be removed from the environment")
        if self.agent_logs and Path(self.agent_logs).is_dir():
            safe_copy(Path(self.agent_logs), trial.paths.agent_dir, self.secrets)

    def out_dir(self) -> str:
        """A private folder of this machine for what the phase's end keeps (a stream's output, scrubbed)."""
        folder = Path(self.trial.paths.trial_dir).parent / f".hermit-kept-{self.mark}"
        folder.mkdir(mode=0o700, exist_ok=True)
        return str(folder)

    async def done_op(self, req: dict) -> dict:
        if self.run_task is None:
            raise RuntimeError("the trial is not started")
        if not self.done.is_set():
            self.agent_logs = req.get("agent_logs")
            self.done.set()
        failed = None
        try:
            await asyncio.shield(self.run_task)
        except BaseException as exc:                     # noqa: BLE001 - the trial itself raised (its finalize)
            failed = exc
        trial = self.trial
        scrub_tree(trial.paths.trial_dir, self.secrets)  # Harbor's scrub ran in its trial; the agent's own too
        try:
            result = json.loads(trial.paths.result_path.read_text())
        except (OSError, ValueError):                    # result.json not written (a full volume): the trial's own
            result = json.loads(trial.result.model_dump_json()) if trial.result is not None else {}
        rewards = (result.get("verifier_result") or {}).get("rewards")
        info = result.get("exception_info") or None
        if failed is not None and not rewards and not info:
            raise HarborDriverError(f"the trial ended with no verdict: {type(failed).__name__}: {failed}")

        def said(kind, message):
            return {"type": kind, "message": scrubbed(str(message or "").encode(), self.secrets).decode()}
        exception = said(info.get("exception_type"), info.get("exception_message")) if info else None
        # every exception after the one Harbor kept (it records the first): a verifier that failed after the agent's
        # timeout is read by its own policy
        later = [said(type(e).__name__, str(e)) for e in self.exceptions[1:]]
        return {"rewards": rewards or None, "exception": exception, "later": later,
                "verifier_dir": str(trial.paths.verifier_dir),
                "verifier_mode": resolve_task_verifier_mode(trial.task.config).value,
                "trial_dir": str(trial.paths.trial_dir)}

    async def stop(self, req: dict | None = None) -> dict:
        """A trial still running cancelled (Harbor's own teardown: its environment down, result.json written), the
        clients of its start ended first (a compose up never outlives the down); never behind another request."""
        task = self.run_task
        if task is not None and not task.done():
            await end_clients("lifecycle")
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        if not self.phase_started.done():                # a start waiting on it: answered with the stop
            self.phase_started.cancel()
        await end_clients()
        if self.trial is not None:
            scrub_tree(self.trial.paths.trial_dir, self.secrets)
            return {"result": str(self.trial.paths.result_path)}
        return {"result": None}

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
        self.running.add(mark)
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
        if req.get("stream"):                            # its output kept there too, as it runs (the phase's end
            path = f"/tmp/hermit-stream-{mark}"          # keeps it in the agent logs if it is still running then)
            self.streams[mark] = (path, str(req["stream"]))
            command = f"set -o pipefail; {{ {command} ; }} | tee {shlex.quote(path)}"
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
        kept = self.streams.pop(mark, None)              # it ended here: its output is the reply's
        if kept is not None:
            with contextlib.suppress(Exception):
                await env.exec(f"rm -f {shlex.quote(kept[0])}", user="root")
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
        """What Hermit provisioned (an upload lands as root's), the agent user's: its uid:gid as the environment says it
        (an image's own USER too), the chown confirmed (the runner's _owner and its chown)."""
        if self.owner is not None:
            done = await self.env.exec(f"chown -R {shlex.quote(self.owner)} {shlex.quote(target)}", user="root")
            if done.return_code != 0:
                raise RuntimeError(f"{target} could not be given to the agent user {self.owner}: "
                                   f"{(done.stdout or '')[-300:]}")

    async def _root(self, command: str) -> None:
        done = await self.env.exec(command, user="root")
        if done.return_code != 0:
            raise RuntimeError(f"{command[:120]}: {(done.stdout or '')[-300:]}")

    async def upload(self, req: dict) -> dict:
        source, target = Path(req["source"]), str(req["target"])
        if req.get("as_agent") and self.owner is not None:
            return await self._upload_as_agent(source, target)
        parent = str(Path(target).parent)
        await self.env.exec(f"mkdir -p {shlex.quote(parent)}", user="root")
        if source.is_dir() and not source.is_symlink():
            await self.env.upload_dir(source, target)
        else:
            await self.env.upload_file(source, target)
        await self._own(target)
        return {}

    async def _upload_as_agent(self, source: Path, target: str) -> dict:
        """An upload the agent's process asked for (the sandbox's proxy), with the agent user's own permissions: the
        files copied as root into a folder of their own, given to that user, and copied into place as that user;
        only folders missing on the way are made (by root, under a real folder) and given to it, so nothing there
        already is reached past its permissions."""
        stage = f"/tmp/hermit-xfer-{uuid.uuid4().hex}"
        payload = f"{stage}/payload"
        is_dir = source.is_dir() and not source.is_symlink()
        q, owner = shlex.quote, shlex.quote(self.owner)
        try:
            await self._root(f"mkdir -m 700 {q(stage)}")
            if is_dir:
                await self.env.upload_dir(source, payload)
            else:
                await self.env.upload_file(source, payload)
            await self._root(f"chown -R {owner} {q(stage)}")
            first = target if is_dir else str(Path(target).parent)
            await self._root(f'missing=; p={q(first)}; while [ ! -e "$p" ] && [ ! -L "$p" ]; do '
                             f'missing="$p $missing"; p=$(dirname "$p"); done; for c in $missing; do '
                             f'[ -L "$(dirname "$c")" ] && exit 3; mkdir "$c" && chown {owner} "$c" || exit 1; done')
            copy = f"cp -Rp {q(payload)}/. {q(target)}/" if is_dir else f"cp -p {q(payload)} {q(target)}"
            done = await self.env.exec(copy)             # the agent user's (the environment's default user)
            if done.return_code != 0:
                raise Failed("PermissionError", f"the agent user may not write {target}: "
                                                f"{(done.stdout or '')[-300:]}")
        finally:
            with contextlib.suppress(Exception):
                await self.env.exec(f"rm -rf {q(stage)}", user="root")
        return {}

    async def download(self, req: dict) -> dict:
        source, target = str(req["source"]), Path(req["target"])
        if req.get("as_agent") and self.owner is not None:
            return await self._download_as_agent(source, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if await self.env.is_dir(source, user="root"):
            await self.env.download_dir(source, target)
        else:
            await self.env.download_file(source, target)
        return {}

    async def _download_as_agent(self, source: str, target: Path) -> dict:
        """A download the agent's process asked for, read with the agent user's own permissions: a tar of it made as
        that user in the environment (tar's own exit decides), unpacked here as data (no link out, no device, no
        absolute path)."""
        import base64
        import tarfile
        parent, name = str(Path(source).parent), Path(source).name
        done = await self.env.exec(
            f'out=$(mktemp) && cd {shlex.quote(parent)} && tar -cf "$out" -- {shlex.quote(name)} && base64 "$out"; '
            'rc=$?; rm -f "$out"; exit $rc')
        if done.return_code != 0:
            raise Failed("PermissionError", f"the agent user may not read {source}: "
                                            f"{(done.stderr or done.stdout or '')[-300:]}")
        with tempfile.TemporaryDirectory(prefix="hermit-down-") as tmp:
            archive = Path(tmp) / "a.tar"
            archive.write_bytes(base64.b64decode("".join((done.stdout or "").split())))
            with tarfile.open(archive) as tar:
                tar.extractall(Path(tmp) / "x", filter="data")
            got = Path(tmp) / "x" / name
            if got.is_dir() and not got.is_symlink():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(got, target, symlinks=True, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or target.exists():
                    target.unlink()
                shutil.move(str(got), str(target))
        return {}

    async def _kill(self, mark: str) -> None:
        """Every process of the agent command `mark` ended, confirmed (kill_marked's exit), from `/`; one not
        confirmed is kept (`unkilled`) and raised: the bridge's run then fails, and Harbor runs no verifier."""
        try:
            done = await self.env.exec(kill_marked(mark), cwd="/", user="root")
            ok = done.return_code == 0
        except Exception as exc:                         # noqa: BLE001 - as unconfirmed
            ok, done = False, exc
        if not ok:
            self.unkilled.add(mark)
            raise HarborDriverError(f"an agent command's processes could not be ended ({mark}): {str(done)[-300:]}")
        self.unkilled.discard(mark)


    # --- the loop -----------------------------------------------------------------------------------------------
    async def handle(self, req: dict) -> None:
        rid, op = req.get("id"), req.get("op")
        try:
            if op == "stop":                             # never behind a start or a done that waits on the trial
                payload = await self.stop(req)
            elif op in ("start", "done"):
                async with self.lock:
                    payload = await getattr(self, "done_op" if op == "done" else op)(req)
            elif op == "secret":                         # a credential file there: removed at the phase's end
                self.secret_files.update(str(p) for p in req.get("paths") or [])
                payload = {}
            elif op in ("exec", "upload", "download"):
                if not self.in_phase:
                    raise Failed("AgentPhaseOver", "the agent's phase is not on (not begun, or ended)")
                task = asyncio.current_task()
                self.agent_ops[task] = op
                CLIENT_KIND.set("agent")                 # this task's own context: its clients are the agent's
                try:
                    payload = await getattr(self, op)(req)
                except asyncio.CancelledError:          # the phase ended: done, Harbor's timeout, a cancellation
                    self.reply(rid, {"ok": False, "error": f"AgentPhaseOver: {op}: the agent's phase ended",
                                     "type": "AgentPhaseOver"})
                    return
                finally:
                    self.agent_ops.pop(task, None)
            else:
                raise ValueError(f"unknown op {op!r}")
            self.reply(rid, {"ok": True, **payload})
        except Exception as exc:                         # noqa: BLE001 - the caller gets the error
            log(f"{op}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}")
            kind = getattr(exc, "kind", None) or type(exc).__name__
            self.reply(rid, {"ok": False, "error": f"{kind}: {exc}"[-4000:], "type": kind})


async def serve(out) -> int:
    global DRIVER
    sys.modules.setdefault(MODULE, sys.modules[__name__])   # Harbor imports the bridge by this name
    driver = DRIVER = Driver(out)
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
    else:                                               # Hermit went away: the trial is cancelled all the same
        with contextlib.suppress(Exception):
            await driver.stop()
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
