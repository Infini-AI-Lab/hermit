"""What every HermitXXX does in a Harbor trial: carry out the launch plan Hermit rendered for its session.

Hermit's side (`hermit.harbor.launch`, in Hermit's venv) renders each trial's session of the acting CLI with the
CLI's own Base Agent (`hermit.agent.base.cli`: prepare_home, home_env, session_env, command), against host staging
paths translated to the container's, and writes it as `<job>/launch/plan.json` (schema version 1). All per-CLI
knowledge stays there; the class Harbor imports (harborside.codex:HermitCodex, ...) is this mixin over Harbor's agent
of that CLI, which brings Harbor's install of the CLI (or uses a mounted binary) and then, in `run`, carries the plan
out the same way for every CLI:

    <WORKDIR>/hermit/<adapter id>/       the adapter's copy (removed, then uploaded)
    <WORKDIR>/<the deployment's files>   what the host wrote beside it (.codex/, .claude/, .pi/, .dsh/, opencode.json,
                                         mini-swe-agent.yaml, ...); a stream's earlier step's files that this
                                         deployment does not replace are removed
    <WORKDIR>/<context file>             Hermit's context first, then the image's own file (captured once per container
                                         in /installed-agent/hermit/original-<name>), within the plan's context_limit
                                         (the image's text counted as the CLI escapes it: context_escaped)
    <WORKDIR>/.hermit-deploy.json        the deployment's record, always (an adapter's hook runner reads its host there)
    /installed-agent/hermit-home/        the CLI's home as Hermit rendered it: @@HERMIT_WORKDIR@@ filled in in its
                                         files, its sign-in files mode 600, all of it the task's agent user's
    /installed-agent/hermit/run/         the launch script, the prompt and the session's pid
    /logs/agent/<stream>                 the session's stdout and stderr (agent-<backend>.jsonl): Hermit reads it with
                                         the CLI's Base Agent after the trial (outcome, parse, steps)
    /logs/agent/hermit-copy/<name>/      the plan's copy_back directories (an adapter's memory), after the session
    /logs/agent/hermit-collect/<to>      the plan's collect files (last_message.txt, a session's database)

A link in the deployment or the home is uploaded as the link it is, as `docker cp` keeps the adapter's own copy: it is
never followed to a file of this host (an evolved adapter's link would otherwise carry that file into the container).

The command runs as the task's agent user, in a process group of its own (setsid; its pid in a file), from <WORKDIR>,
with nvm loaded when the plan says so (with the user's own HOME, before a variable of the plan replaces it), the
class's install directories and the plan's own first on PATH, the plan's variables, HERMIT_HOOK_LOGS=/logs/agent (where
an adapter's hooks write their audit and state, which come back with the trial), and the prompt (Harbor's instruction
and the plan's prompt_suffix, with its prompt_slash_prefix before one that starts with `/`) in its arguments
(@@HERMIT_PROMPT@@) or on its stdin, read from a file in the container.

A key or a token never leaves Harbor's environment by value (Codex's ChatGPT sign-in is a file of the plan's home, mode
600): an `env_from_host` variable reaches `docker compose exec` by name
(`-e NAME`: the compose client reads the value from its own environment, Harbor's), and an `env_by_fd` one (Claude
Code's subscription token) goes on into a pipe on descriptor 3 (the CLI reads it from there once), never in the
environment of the launch script or of anything it starts. Neither is in a command line, a file or a log; only a
local Docker environment can pass a variable by name, so a plan with secrets is refused elsewhere.

However the session ends (its exit, a non-zero exit, Harbor's timeout cancelling `run`), a session still running is
killed with its group (TERM, the plan's grace, KILL): Harbor's timeout cancels only Harbor's wait on the
`docker compose exec` client, and a CLI left running would go on editing under the verifier. On Harbor's default path
(an exec with no log callback streaming the trial's output, as under `harbor run`, and every `docker compose cp`) a
client whose wait was cancelled goes on running on this host, as Harbor ends one only when the call's own timeout_sec
runs out (the session's exec has none), and a client Harbor ends (an exec's with a log callback, on the cancellation as
well) leaves the CLI running in the container all the same. A session that ended by itself leaves what it started in
the background running (a server the task asked for), as Harbor's own agents and Hermit's own sessions do. Then the
copy-back, the collected files and the files returned to the host (a refreshed ChatGPT sign-in) are taken, each on its
own and from / (Docker refuses an exec in a workdir the session removed), so that none of them hides the session's own
error, and Harbor's timeout landing in one of them does not keep the others from running (the one it cut short is
not run again, but for the return of the files, which runs once more: its copy may overlap the cancelled one's, a
`docker compose cp` left running on this host that writes the same file), nor does it hide that error: the
cancellation goes on after the last, unless the session raised an error of its own, which then goes on as it is
(Harbor's wait_for turns only a cancellation into the agent's timeout, so a launch failure or a non-zero exit is not
taken for one). Once the run is cancelled, each of them has AFTER_CANCEL_S at most (a hung container would hold
them), and a second cancellation (the job's, after Harbor's timeout) goes on at once. A non-zero exit raises
Harbor's NonZeroAgentExitCodeError (the verifier still runs; Hermit's side reads the session's verdict from its
stream). What keeps the session from starting that is not the agent's doing (no plan, secrets by name outside a local
Docker environment or missing from Harbor's environment, no agent user, a CLI that cannot be installed or run there, one
of harborside's own steps before the session's exec failing: an upload, a chown, a probe of the container; a launch
script that cannot enter the workdir; a non-zero exit that left no stream, which the launch script makes before the CLI
starts (a stream's earlier step's is removed before the exec), as when the exec failed in Docker; the session's exec
this machine could not run (an OSError: the compose client not spawned, EMFILE, EAGAIN), and, after a non-zero exit, a
look for its stream it could not run: whether the CLI started cannot be told) raises HermitLaunchError, which Hermit's
side counts as infra; a context file over the CLI's limit once merged with the image's own raises
HermitContextLimitError, the adapter's result for the task (hermit.harbor.errors).

Harbor's model connection is not used: the model, its endpoint and its key are the plan's. MODEL_CONNECTION is None,
so Harbor resolves no key, infers no endpoint and allowlists no host from one (Hermit's runner names the hosts), and a
base_url, an API key name or an API format given to the agent is refused. Nor are Harbor's readers of the CLI's own
logs: `populate_context_post_run` records metadata only (the CLI, whether an adapter was deployed and its id, whether
hooks and python3 were there, a mounted binary, the exit code, whether the session had to be killed, the session's
stream, the after-steps that failed or were cancelled (after_session_errors), and the plan's own metadata).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tempfile
from typing import Any, ClassVar

from harbor.agents.agent_auth_resolution import ResolvedModelConnection
from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, NonZeroAgentExitCodeError, with_prompt_template
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from pydantic import Field, field_validator

PLAN_VERSION = 1
CLIS = ("codex", "claude", "pi", "opencode", "dsh", "mini-swe-agent", "answer")
HOME = "/installed-agent/hermit-home"            # every CLI's home in the container
STATE = "/installed-agent/hermit"                 # harborside's own: the image's original context files, run/, bin/
RUN = f"{STATE}/run"                              # the launch script, the prompt, the pid (the agent user's)
PIDFILE = f"{RUN}/session.pid"
TOOLS_BIN = f"{STATE}/bin"                        # the hermit-tool script (tools_script)
LOGS = "/logs/agent"                              # Harbor's agent log directory: it comes back with the trial
COPY_DIR = f"{LOGS}/hermit-copy"
COLLECT_DIR = f"{LOGS}/hermit-collect"
WORKDIR_MARK = "@@HERMIT_WORKDIR@@"
PROMPT_MARK = "@@HERMIT_PROMPT@@"
PROMPT_VAR = "HERMIT_PROMPT_TEXT"                 # the launch script's variable that holds the prompt (not exported)
ADAPTER_DIR = "hermit"
DEPLOY_RECORD = ".hermit-deploy.json"
HOOK_LOGS = "HERMIT_HOOK_LOGS"                    # where an adapter's hook runner writes its audit and state
LAUNCH_FAILED = 97                                # the launcher's exit code when it cannot enter the workdir
CANNOT_ENTER = "hermit: cannot enter the workdir"  # ... and what it says then: both, not a CLI's own exit 97
FIRST_FD = 3                                      # env_by_fd's descriptors: 3, then 4, ...
DEFAULT_KILL_GRACE_S = 10
AFTER_CANCEL_S = 120                              # an after-step's time at most once the run was cancelled
COMPOSE_EXEC = ("_run_docker_compose_command", "_merge_env", "_resolve_user", "_output_callback")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)")
# Harbor's own test for a musl image, made to hold under pipefail (the prefix of Harbor's exec_as_root and
# exec_as_agent; dsh's install sets its own): musl's ldd says what it is and exits 1
MUSL = "{ ldd --version 2>&1 || true; } | grep -qi musl || [ -f /etc/alpine-release ]"
# A value read from the container is printed on a line of its own after its mark, and `marked` takes it from there:
# Harbor's docker environment merges an exec's stderr into its stdout, so other lines (a compose warning, an echo of
# the image's BASH_ENV) may come before and after it. A command's whole output (what a mounted binary says to
# --version) is printed between a begin and an end line of its own, and `between` takes it from there.
OWNER_LINE = "hermit-owner:"
PWD_LINE = "hermit-pwd:"
CONTEXT_LINE = "hermit-context:"                  # the image's own context file, in base64
RETURN_LINE = "hermit-return:"                    # whether a file to return is there: yes (a file, no link), or no
VERSION_BEGIN = "hermit-version-begin"            # what a mounted binary says to --version, between these lines
VERSION_END = "hermit-version-end"


class HermitLaunchError(RuntimeError):
    """What keeps the launch plan's session from starting that is not the agent's doing: no plan, secrets by name
    outside a local Docker environment or missing from Harbor's, a container without the task's agent user, a CLI
    that cannot be installed (its install failed: a download, a package, an image it refuses) or run there, a step of
    harborside's own before the session's exec that failed (an upload, a chown, a probe of the container), a launch
    script that could not enter the workdir, a non-zero exit that left no stream (the CLI never started, as when the
    exec failed in Docker), the session's exec this machine could not run (an OSError: the compose client not
    spawned, EMFILE, EAGAIN), and, after a non-zero exit, a look for its stream it could not run (whether the CLI
    started cannot be told). Hermit's side counts the trial as infra (run again), not as the agent's 0."""


class HermitContextLimitError(RuntimeError):
    """The deployment's context file, merged with the image's own, is over the CLI's limit: the adapter's result for
    the task (its context too long beside the task's own), scored as the agent's."""


@contextlib.contextmanager
def _setup_step(what: str):
    """One of harborside's own steps before the session's exec starts: an exception there (an exec of Harbor's that
    exited non-zero, a `docker compose cp` that failed) is not the agent's doing, so it goes on as HermitLaunchError
    saying what failed; HermitLaunchError, HermitContextLimitError and a cancellation go on as they are."""
    try:
        yield
    except (HermitLaunchError, HermitContextLimitError):
        raise
    except Exception as exc:                               # noqa: BLE001 - harborside's own, whatever it was
        raise HermitLaunchError(f"hermit: {what} failed: {type(exc).__name__}: {exc}") from exc


async def _bounded(work) -> None:
    """An after-step once the run was cancelled: AFTER_CANCEL_S at most (a hung container would hold it for good),
    then a RuntimeError saying so; a cancellation goes on as it is."""
    try:
        await asyncio.wait_for(work, AFTER_CANCEL_S)
    except asyncio.TimeoutError:
        raise RuntimeError(f"not done within {AFTER_CANCEL_S} s of the run's cancellation") from None


# The hermit-tool command for a CLI whose model runs bash commands only (mini-swe-agent): an adapter's tools, served
# by Hermit's tool server on the host, as one command. It answers as hermit.agent.base.mini_swe_agent_env answers the
# same command inside Hermit's environment class (which intercepts it before bash when that class runs), except when
# GET <url>/tools answers with no list of tools (an HTTP error's JSON object, a list of anything but objects with a
# name and a description): here that is an `Error: RuntimeError: ...` line.
TOOL_SCRIPT = '''#!__PYTHON__
"""hermit-tool: the adapter's tools as one command (written by harborside for the launch plan's tools_script).

    hermit-tool                             the tools, one per line
    hermit-tool <name> '<JSON arguments>'   the tool's result; an error exits 1

The tools are the trial's tool server on the host, at the URL in $__URL_ENV__ (GET <url>/tools, POST <url>/call/<name>).
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("__URL_ENV__", "").rstrip("/")


def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return json.loads(error.read() or b"{}")


def text(result):
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


def main(words):
    try:
        tools = request(URL + "/tools") if URL else []
        # Anything but a list of tools (objects with a name and a description) is not the tools (an HTTP error's JSON
        # object): the error said is the object's "error" (or FastAPI's "detail"), else the answer itself
        if not isinstance(tools, list) or not all(isinstance(t, dict) and "name" in t and "description" in t
                                                  for t in tools):
            raise RuntimeError(tools.get("error", tools.get("detail", tools)) if isinstance(tools, dict) else tools)
    except Exception as exc:
        return "Error: %s: %s" % (type(exc).__name__, exc), 1
    if not tools:
        return "Error: no tools are available here", 1
    if not words:
        return "\\n".join("%s: %s" % (t["name"], t["description"]) for t in tools), 0
    if len(words) > 2:
        return ("Error: run hermit-tool as a command of its own: hermit-tool <name> '<JSON arguments>', with the "
                "arguments as one JSON object in single quotes"), 1
    try:
        if words[0] not in {t["name"] for t in tools}:
            raise ValueError("Unknown tool: " + words[0])
        arguments = json.loads(words[1]) if len(words) > 1 else {}
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
        answer = request(URL + "/call/" + words[0], arguments)
        if "error" in answer:
            raise RuntimeError(answer["error"])
        return text(answer.get("result")), 0
    except Exception as exc:
        return "Error: %s: %s" % (type(exc).__name__, exc), 1


if __name__ == "__main__":
    out, code = main(sys.argv[1:])
    print(out)
    sys.exit(code)
'''


def _q(value: str) -> str:
    return shlex.quote(str(value))


def marked(output: str | None, mark: str) -> str | None:
    """The value a command printed after `mark` (`echo "<mark><value>"`), from the last line that carries the mark,
    stripped; None when no line does. Other lines of the exec's output (its stderr merged in) are not the value."""
    lines = [line for line in (output or "").splitlines() if mark in line]
    return lines[-1].split(mark, 1)[1].strip() if lines else None


def between(output: str | None, begin: str, end: str) -> str | None:
    """What a command printed between a line `begin` and a line `end` it printed around it (`echo <begin>;
    <command>; printf '\\n<end>\\n'`), after the last line `begin`; None when no such lines are there. Other lines of
    the exec's output (its stderr merged in) are not the command's."""
    lines = (output or "").splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    rest = lines[begins[-1] + 1:] if begins else []
    ends = [i for i, line in enumerate(rest) if line.strip() == end]
    return "\n".join(rest[:ends[0]]) if ends else None


def _relative(path: str) -> bool:
    """A relative POSIX path that stays below where it is put (no `..`, not absolute, not empty)."""
    parts = PurePosixPath(str(path)).parts
    return bool(parts) and not PurePosixPath(str(path)).is_absolute() and ".." not in parts


def _absolute(path: str) -> bool:
    """An absolute container path, or one under the workdir's placeholder."""
    text = str(path)
    return text.startswith("/") or text.startswith(WORKDIR_MARK)


def _regular(path: Path) -> bool:
    """A regular file reached through no link: what harborside reads on the host. A link in a deployment or a home
    is uploaded as the link it is (as `docker cp` keeps the adapter's own), never followed to a file of this host."""
    return path.is_file() and not path.is_symlink()


def _copy_tree(source: Path, target: Path, workdir: str, *, left_out: set = frozenset(),
               unfilled: set = frozenset()) -> set:
    """`source`'s entries but `left_out` (names at its top) copied to `target`, links as links, @@HERMIT_WORKDIR@@
    filled in in the regular files but `unfilled` (paths relative to `source`); the copied files' and links' paths
    relative to `target`."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        if entry.name in left_out:
            continue
        if entry.is_symlink():
            (target / entry.name).symlink_to(os.readlink(entry))
        elif entry.is_dir():
            shutil.copytree(entry, target / entry.name, symlinks=True)
        elif entry.is_file():
            shutil.copy2(entry, target / entry.name)
    entries = set()
    for path in sorted(target.rglob("*")):                 # a link to a folder is an entry, not descended into
        rel = path.relative_to(target).as_posix()
        if _regular(path) and rel not in unfilled:
            data = path.read_bytes()
            if WORKDIR_MARK.encode() in data:
                path.write_bytes(data.replace(WORKDIR_MARK.encode(), workdir.encode()))
        if path.is_symlink() or path.is_file():
            entries.add(rel)
    return entries


def plan_problems(plan: dict) -> list[str]:
    """What is wrong with a launch plan's fields (schema version 1), as messages naming the field; [] when none.
    The answer agent's own block is HermitAnswer's to check."""
    problems: list[str] = []

    def names(field: str, value: Any, *, values_are_names: bool) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            problems.append(f"{field} must be a mapping")
            return
        for key, item in value.items():
            if not isinstance(key, str) or not NAME.fullmatch(key):
                problems.append(f"{field} has a variable name that is not one: {key!r}")
            if not isinstance(item, str) or (values_are_names and not NAME.fullmatch(item)):
                problems.append(f"{field}[{key}] must be {'a variable name' if values_are_names else 'a string'}")

    cli = plan.get("cli")
    if cli != "answer":
        command = plan.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            problems.append("command must be a non-empty list of strings")
        stream = plan.get("stream")
        if not isinstance(stream, str) or not stream or "/" in stream or stream in (".", ".."):
            problems.append("stream must be a file name in /logs/agent")
        home = plan.get("home")
        if not isinstance(home, dict) or not home.get("host_dir"):
            problems.append("home must name its host_dir")
        else:
            if not Path(str(home["host_dir"])).is_dir():
                problems.append(f"home.host_dir is not a directory: {home['host_dir']}")
            if not str(home.get("container_dir") or HOME).startswith("/"):
                problems.append("home.container_dir must be an absolute container path")
            for name in home.get("secret_files") or []:
                if not _relative(name):
                    problems.append(f"home.secret_files must be paths relative to the home, not {name!r}")
    names("env", plan.get("env"), values_are_names=False)
    names("env_from_host", plan.get("env_from_host"), values_are_names=True)
    names("env_by_fd", plan.get("env_by_fd"), values_are_names=True)
    deploy = plan.get("deploy")
    if deploy is not None:
        if not isinstance(deploy, dict) or not deploy.get("work_dir"):
            problems.append("deploy must name its work_dir")
        else:
            if not Path(str(deploy["work_dir"])).is_dir():
                problems.append(f"deploy.work_dir is not a directory: {deploy['work_dir']}")
            name = deploy.get("context_file")
            if name is not None and (not isinstance(name, str) or not _relative(name) or "/" in name):
                problems.append(f"deploy.context_file must be a file name, not {name!r}")
            limit = deploy.get("context_limit")
            if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
                problems.append("deploy.context_limit must be a positive number of bytes or null")
            escaped = deploy.get("context_escaped")
            if escaped is not None and (not isinstance(escaped, str) or not escaped):
                problems.append("deploy.context_escaped must be the text the CLI escapes by a byte, or null")
            for rel in deploy.get("copy_back") or []:
                if not isinstance(rel, str) or not _relative(rel):
                    problems.append(f"deploy.copy_back must hold paths relative to the workdir, not {rel!r}")
    bundle = plan.get("bundle")
    if bundle is not None and (not isinstance(bundle, dict) or not Path(str(bundle.get("host_dir") or "")).is_dir()
                               or not str(bundle.get("container_dir") or "").startswith("/")):
        problems.append("bundle must name an existing host_dir and an absolute container_dir")
    for entry in plan.get("collect") or []:
        if not isinstance(entry, dict) or not _absolute(entry.get("from", "")) or not _relative(entry.get("to", "")):
            problems.append(f"collect entries are {{from: a container path, to: a path relative to "
                            f"{COLLECT_DIR}}}, not {entry!r}")
    for entry in plan.get("return_files") or []:
        if not isinstance(entry, dict) or not _absolute(entry.get("from", "")) or \
                not Path(str(entry.get("to", ""))).is_absolute():
            problems.append(f"return_files entries are {{from: a container path, to: an absolute host path}}, "
                            f"not {entry!r}")
    if not isinstance(plan.get("path_prepend") or [], list):
        problems.append("path_prepend must be a list of directories")
    if not isinstance(plan.get("prompt_slash_prefix") or "", str):
        problems.append("prompt_slash_prefix must be text or null")
    tools = plan.get("tools_script")
    if tools is not None and (not isinstance(tools, dict) or not NAME.fullmatch(str(tools.get("url_env") or ""))):
        problems.append("tools_script must name its url_env variable")
    grace = plan.get("kill_grace_s", DEFAULT_KILL_GRACE_S)
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace < 0:
        problems.append("kill_grace_s must be a number of seconds")
    if not isinstance(plan.get("metadata") or {}, dict):
        problems.append("metadata must be a mapping")
    return problems


def load_plan(path: str | Path, cli: str | None = None) -> dict:
    """The launch plan at `path` (schema version 1), checked; with `cli`, it must be that CLI's."""
    source = Path(str(path)).expanduser()
    try:
        plan = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"hermit: the launch plan {source} cannot be read: {exc}") from None
    if not isinstance(plan, dict):
        raise ValueError(f"hermit: the launch plan {source} is not a JSON object")
    if plan.get("version") != PLAN_VERSION:
        raise ValueError(f"hermit: the launch plan {source} is version {plan.get('version')!r} and this harborside "
                         f"reads version {PLAN_VERSION}: render it with the matching Hermit")
    if plan.get("cli") not in CLIS:
        raise ValueError(f"hermit: the launch plan {source} names cli {plan.get('cli')!r}, not one of "
                         f"{', '.join(CLIS)}")
    if cli is not None and plan["cli"] != cli:
        raise ValueError(f"hermit: the launch plan {source} is for {plan['cli']}, and this agent runs {cli}: give the "
                         "agent its own CLI's plan (hermit.harbor.clis names each CLI's class)")
    problems = plan_problems(plan)
    if problems:
        raise ValueError(f"hermit: the launch plan {source} is not usable: " + "; ".join(problems))
    return plan


class HermitOptions(InstalledAgentOptions):
    """The kwargs every HermitXXX takes besides its Harbor class's. `extra="forbid"` is inherited: anything else is
    refused at Harbor's preflight."""

    plan: str | None = Field(default=None, description="The launch plan Hermit rendered for the trial (plan.json on "
                                                        "this machine): the deployment, the CLI's home, its command.")
    image_repairs: bool = Field(default=True, description="Repair the apt sources of images whose release left the "
                                                          "mirrors (Debian bullseye) before a package is installed.")
    ensure_python3: bool = Field(default=True, description="Install python3 when the deployment has hooks and the "
                                                           "image lacks it (best effort).")

    @field_validator("plan")
    @classmethod
    def _plan_is_a_file(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).expanduser().is_file():
            raise ValueError(f"the launch plan {value} is not a file")
        return value


class HermitInstalled(BaseInstalledAgent):
    """The generic executor of a launch plan (the module's docstring), mixed in first over Harbor's agent of the CLI:
    `class HermitCodex(HermitInstalled, Codex)`. A subclass names its CLI and what differs in its install."""

    CLI: ClassVar[str] = ""                       # the plan's `cli`
    PROGRAM: ClassVar[str] = ""                   # the CLI's program: a mounted binary is linked as /usr/local/bin/<it>
    BIN_OPTION: ClassVar[str | None] = None       # the option naming a mounted binary (codex_bin, ...)
    RUN_PATH: ClassVar[tuple[str, ...]] = ()      # where the class's install puts the CLI: first on PATH, before the
                                                  # plan's variables ($HOME is the agent user's own there)
    MODEL_CONNECTION = None                       # the model is the plan's (the module's docstring)
    # Hermit's plan sets web search either way, off for a task whose agent network is not public (the runner's rule,
    # Harbor's own), so Harbor's switch has nothing left to do; no skills or MCP servers of Harbor's are wired (the
    # adapter's are the plan's), so a task that names some is refused by Harbor, not run without them.
    capabilities = AgentCapabilities(native_web_tools=True)
    options_model = HermitOptions
    options: HermitOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._plan: dict | None = None
        self._mounted = False
        self._python3_ok: bool | None = None
        self._apt_repaired = False
        self._hooks_active = False
        self._placed: dict = {"files": set(), "copies": set(), "context": None, "record": False}
        self._facts: dict = {}
        if self.options.plan:
            self.set_plan(self.options.plan)

    # ----- the plan ---------------------------------------------------------------------------------------------------

    def set_plan(self, path: str | None) -> None:
        """The plan the next `run` carries out (a stream's driver gives each step its own); None: no plan."""
        self._plan = load_plan(path, self.CLI) if path else None

    @classmethod
    def resolve_model_connection_config(cls, model_name, resolve_env, **options):
        """No connection of Harbor's: the plan holds the model, its endpoint and its key."""
        given = [k for k in ("base_url", "model_api_key_name", "model_api_format") if options.get(k) is not None]
        if given:
            raise ValueError(f"Agent '{cls.name()}' takes its model from Hermit's launch plan: give it no "
                             f"{', '.join(given)}")
        return ResolvedModelConnection()

    @classmethod
    def check_model_credentials(cls, connection, **_: Any) -> None:
        """Nothing to check before the trial: the plan's secrets are checked by name when the session starts."""

    async def _refresh_web_tool_config(self, environment: BaseEnvironment) -> None:
        """Nothing written to refresh: web search is the plan's."""

    @property
    def _bin(self) -> str | None:
        return getattr(self.options, self.BIN_OPTION, None) if self.BIN_OPTION else None

    # ----- install ----------------------------------------------------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        """The mounted binary when it runs here as the asked-for version and the task's agent user may execute it,
        else the class's install (after the apt repair of an image whose release left the mirrors). A failure in any
        of it (the mount's probe, a package, an npm or GitHub download, an image the CLI refuses) is not the agent's
        doing: HermitLaunchError, which Harbor's setup passes on as it is."""
        with _setup_step(f"installing {self.CLI}"):
            if self._bin and await self._use_mounted(environment):
                return
            if self.options.image_repairs:
                await self._repair_apt_sources(environment)
            await self.install_cli(environment)

    async def install_cli(self, environment: BaseEnvironment) -> None:
        """The CLI's own install: Harbor's agent's, unless the class has its own."""
        await super().install(environment)

    async def _use_mounted(self, environment: BaseEnvironment) -> bool:
        """The mounted binary, when it runs here and is the asked-for version (any, when none was asked for), and the
        user the session runs as may execute it: linked onto PATH (as root), no install. Its version is asked as root
        (Codex under a HOME it cannot write, as an agent user's may be, warns first, a line Harbor's parse_version
        would take for the version); whether it may be executed is asked as the task's agent user (Harbor's default
        user for the install as for the session, unless a step of a multi-step task names its own) with `test -x`,
        which needs search permission on every folder on the way: a mount that user may not execute (its mode and
        owner are this machine's) is not used. Otherwise the class's install, and the mount is left off PATH. What it
        says to --version is read between lines of its own (`between`), not among the exec's other lines."""
        binary = PurePosixPath(str(self._bin))
        got = await environment.exec(command=f"echo {VERSION_BEGIN}; {_q(binary)} --version 2>&1; c=$?; "
                                             f"printf '\\n{VERSION_END}\\n'; exit \"$c\"", user="root")
        said = between(got.stdout, VERSION_BEGIN, VERSION_END)
        version = self.parse_version(said) if got.return_code == 0 and (said or "").strip() else None
        if not version or (self._version and version != self._version):
            self.logger.warning("The mounted %s %s does not run here as %s (%s): installing it", self.CLI, binary,
                                self._version or "any version", (said or got.stdout or got.stderr or "").strip()[:200])
            return False
        if (await environment.exec(command=f"test -x {_q(binary)}")).return_code != 0:
            self.logger.warning("The mounted %s %s may not be executed here by the session's user: installing it",
                                self.CLI, binary)
            return False
        await self.exec_as_root(environment, command=f"mkdir -p /usr/local/bin && ln -sf {_q(binary)} "
                                                     f"/usr/local/bin/{_q(self.PROGRAM)}")
        await self._after_mount(environment, binary)
        self._mounted = True
        return True

    async def _after_mount(self, environment: BaseEnvironment, binary: PurePosixPath) -> None:
        """What a mounted binary needs besides its link (nothing, unless the class says so)."""

    async def _repair_apt_sources(self, environment: BaseEnvironment) -> None:
        """Debian 11 (bullseye) images: the bullseye pools on deb.debian.org no longer serve the release's packages,
        so an `apt-get install` (curl for an install, python3 for hooks) fails and the trial dies before the CLI
        starts. Point main and security at archive.debian.org, accept its expired Release files, refresh the lists,
        once per agent. Other images are untouched; a failure here is left to the install to report."""
        if self._apt_repaired:
            return
        self._apt_repaired = True
        command = ("if grep -qs 'VERSION_CODENAME=bullseye' /etc/os-release && command -v apt-get >/dev/null 2>&1; "
                   "then sed -i "
                   "-e 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
                   "-e 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' "
                   "-e 's|http://deb.debian.org/debian |http://archive.debian.org/debian |g' /etc/apt/sources.list "
                   "/etc/apt/sources.list.d/*.list 2>/dev/null; "
                   "echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99hermit-archive; "
                   "apt-get update; fi")
        try:
            await self.exec_as_root(environment, command=command)
        except Exception as exc:                           # noqa: BLE001 - reported by the install if it matters
            self.logger.warning("apt source repair skipped: %s", exc)

    async def _ensure_python3(self, environment: BaseEnvironment) -> bool:
        """python3 on PATH, installed with the image's package manager when there is one. False, never an
        exception: a container without Python runs the trial with hooks that may fail open."""
        check = "command -v python3 >/dev/null 2>&1"
        if (await environment.exec(command=check)).return_code == 0:
            return True
        if self.options.image_repairs:
            await self._repair_apt_sources(environment)
        try:
            await self.ensure_system_dependencies(environment, ("python3",))
        except Exception as exc:                           # apt failure, no network: fall back, do not fail the trial
            self.logger.warning("Installing python3 failed: %s", exc)
            return False
        return (await environment.exec(command=check)).return_code == 0

    # ----- the session ------------------------------------------------------------------------------------------------

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        plan = self._plan
        if plan is None:
            raise HermitLaunchError(f"hermit: {type(self).__name__} runs the launch plan Hermit renders for each trial "
                                    "(kwargs.plan), and it was given none")
        self._facts = {"exit_code": None, "killed": False, "after_session_errors": []}
        self._hooks_active = False                        # this step's deployment's (_deploy), never the step before's
        with _setup_step("finding the workdir"):
            workdir = await self._workdir(environment, plan)
        self._facts["workdir"] = workdir
        cancelled = failed = False
        try:
            with _setup_step("reading the task's agent user"):
                owner = await self._owner(environment)
            with _setup_step("preparing the run directory"):
                await self.exec_as_root(environment, command=f"mkdir -p {_q(RUN)} {_q(TOOLS_BIN)} && "
                                                             f"chown {_q(owner)} {_q(RUN)} && chmod 700 {_q(RUN)} && "
                                                             f"rm -f {_q(PIDFILE)}")
            with _setup_step("deploying the adapter"):
                await self._deploy(environment, plan, workdir, owner)
            with _setup_step("uploading the CLI's home"):
                await self._upload_home(environment, plan, workdir, owner)
            with _setup_step("uploading the bundle"):
                await self._upload_bundle(environment, plan)
            if plan.get("tools_script"):
                with _setup_step("installing the hermit-tool script"):
                    await self._install_tool_script(environment, plan)
            await self._launch(environment, plan, workdir, prompt_of(plan, instruction), owner)
        except asyncio.CancelledError:
            cancelled = True                              # Harbor's timeout: the after-steps have a bounded time
            raise
        except Exception:
            failed = True                                 # the session's own error: it stands after the after-steps
            raise
        finally:
            await self._after_session(environment, plan, workdir, cancelled=cancelled, failed=failed)

    async def _workdir(self, environment: BaseEnvironment, plan: dict) -> str:
        """Where the session runs: the plan's workdir (the task's [environment].workdir), else where Harbor's execs
        start (`pwd` as the agent user)."""
        workdir = plan.get("workdir")
        if not workdir:
            said = (await self.exec_as_agent(environment, command=f'echo "{PWD_LINE}$(pwd)"')).stdout
            workdir = marked(said, PWD_LINE) or "/"
        return str(workdir).rstrip("/") or "/"

    async def _owner(self, environment: BaseEnvironment) -> str:
        """The task's agent user as uid:gid, for what is uploaded as root and must be that user's."""
        said = (await self.exec_as_agent(environment, command=f'echo "{OWNER_LINE}$(id -u):$(id -g)"')).stdout
        owner = marked(said, OWNER_LINE) or ""
        if not re.fullmatch(r"\d+:\d+", owner):
            raise HermitLaunchError(f"hermit: the task's agent user has no uid:gid here ({(said or '').strip()!r})")
        return owner

    @staticmethod
    def _fill(text: str, workdir: str) -> str:
        return str(text).replace(WORKDIR_MARK, workdir)

    async def _upload_text(self, environment: BaseEnvironment, text: str, remote: str) -> None:
        with tempfile.TemporaryDirectory(prefix="hermit-upload-") as tmp:
            local = Path(tmp) / (PurePosixPath(remote).name or "file")
            local.write_text(text, encoding="utf-8")
            await environment.upload_file(local, remote)

    # ----- the deployment ---------------------------------------------------------------------------------------------

    async def _deploy(self, environment: BaseEnvironment, plan: dict, workdir: str, owner: str) -> None:
        """The deployment into the workdir (the module's docstring), replacing a stream's earlier step's."""
        deploy = plan.get("deploy") or None
        root = workdir.rstrip("/")
        new: dict = {"files": set(), "copies": set(), "context": None, "record": False}
        with tempfile.TemporaryDirectory(prefix="hermit-deploy-") as tmp:
            rest = Path(tmp) / "rest"
            rest.mkdir()
            work = Path(deploy["work_dir"]) if deploy else None
            context = None
            if work is not None:
                adapters = work / ADAPTER_DIR
                new["copies"] = {p.name for p in adapters.iterdir() if p.is_dir() and not p.is_symlink()} \
                    if adapters.is_dir() and not adapters.is_symlink() else set()
                context = deploy.get("context_file")
                if context and _regular(work / context):
                    new["context"] = context
                new["record"] = _regular(work / DEPLOY_RECORD)
                # The rest of the deployment (the host's files beside the copy), with the workdir filled in
                new["files"] = _copy_tree(work, rest, workdir, left_out={ADAPTER_DIR, DEPLOY_RECORD,
                                                                         *([context] if context else [])})
            await self._remove_stale(environment, root, new)
            self._hooks_active = bool(deploy and deploy.get("hooks"))
            if work is None:
                self._placed = new
                return
            if self._hooks_active and self.options.ensure_python3 and self._python3_ok is None:
                self._python3_ok = await self._ensure_python3(environment)
            owned: list[str] = []
            if new["copies"]:
                await self.exec_as_root(environment, command="rm -rf " + " ".join(
                    _q(f"{root}/{ADAPTER_DIR}/{name}") for name in sorted(new["copies"])) +
                    f" && mkdir -p {_q(f'{root}/{ADAPTER_DIR}')}")
                await environment.upload_dir(work / ADAPTER_DIR, f"{root}/{ADAPTER_DIR}")
                owned += [f"{root}/{ADAPTER_DIR}/{name}" for name in sorted(new["copies"])]
            if new["files"]:
                await environment.upload_dir(rest, root or "/")           # a workdir of / is root ""
                tops = {PurePosixPath(f).parts[0] for f in new["files"]}
                owned += sorted(f"{root}/{top}" for top in tops if not (rest / top).is_symlink())
            if new["context"]:
                await self._place_context(environment, deploy, work, root)
                owned.append(f"{root}/{new['context']}")
            if new["record"]:                             # always: an adapter's hook runner finds its host there
                await environment.upload_file(work / DEPLOY_RECORD, f"{root}/{DEPLOY_RECORD}")
                owned.append(f"{root}/{DEPLOY_RECORD}")
            else:
                self.logger.warning("hermit: the deployment in %s has no %s: an adapter's hooks cannot find their host",
                                    work, DEPLOY_RECORD)
            if owned:
                await self.exec_as_root(environment, command=f"chown -R {_q(owner)} {shlex.join(owned)} && "
                                                             f"chmod -R u+rwX {shlex.join(owned)}")
        self._placed = new

    async def _remove_stale(self, environment: BaseEnvironment, root: str, new: dict) -> None:
        """What the last deployment in this container placed and this one does not: removed (a stream's step that
        deploys less than the one before), and the image's own context file put back when this one has none."""
        old = self._placed
        gone = [f"{root}/{f}" for f in sorted(old["files"] - new["files"])]
        gone += [f"{root}/{ADAPTER_DIR}/{c}" for c in sorted(old["copies"] - new["copies"])]
        if old["record"] and not new["record"]:
            gone.append(f"{root}/{DEPLOY_RECORD}")
        commands = [f"rm -rf {shlex.join(gone)}"] if gone else []
        if old["context"] and old["context"] != new["context"]:
            original = f"{STATE}/original-{old['context']}"
            target = _q(f"{root}/{old['context']}")
            commands.append(f"if [ -f {_q(original)} ]; then cat {_q(original)} > {target}; else rm -f {target}; fi")
        if commands:
            await self.exec_as_root(environment, command=" && ".join(commands))

    async def _place_context(self, environment: BaseEnvironment, deploy: dict, work: Path, root: str) -> None:
        """The context file: Hermit's text first, then the image's own file as the container had it before the first
        deployment (captured then, once per container), within the plan's context_limit (the image's text a byte
        longer for each `context_escaped` in it, as the CLI escapes it). An image's own file that cannot be read (no
        base64 or tr there, a read that failed) or captured whole (its copy failed part-way: a full disk) is a launch
        failure (HermitLaunchError), never taken for none (it would be overwritten) and never kept in part (it would be
        merged, and put back as the image's own file by a later step without a context file)."""
        name = deploy["context_file"]
        ours = (work / name).read_text(encoding="utf-8")
        original = f"{STATE}/original-{name}"
        target = f"{root}/{name}"
        # Captured whole or not at all: copied beside it, then renamed into its place; a copy that failed is removed
        part = _q(original + ".part")
        failed = _q(f"hermit: the image's own {name} could not be captured in {original}")
        capture = (f"mkdir -p {_q(STATE)} || exit 3; if [ ! -e {_q(original)} ] && [ ! -e {_q(original + '.none')} ]; "
                   f"then if [ -f {_q(target)} ]; then cat {_q(target)} > {part} && mv -f {part} {_q(original)}; "
                   f"else : > {_q(original + '.none')}; fi || {{ rm -f {part}; echo {failed} >&2; exit 3; }}; fi; "
                   f"v=; if [ -f {_q(original)} ]; then "
                   "command -v base64 >/dev/null 2>&1 && command -v tr >/dev/null 2>&1 || "
                   "{ echo 'hermit: no base64 or tr in the image' >&2; exit 3; }; "
                   f"v=$(set -o pipefail; base64 < {_q(original)} | tr -d '\\n') || exit 3; fi; "
                   f"printf '{CONTEXT_LINE}%s\\n' \"$v\"")
        said = (await self.exec_as_root(environment, command=capture)).stdout
        value = marked(said, CONTEXT_LINE)
        if value is None:                     # never the image's own file taken for none (it would be overwritten)
            raise HermitLaunchError(f"hermit: the image's own {name} could not be read: {(said or '').strip()[-300:]}")
        image = base64.b64decode(value).decode("utf-8", errors="replace")
        merged = ours.rstrip("\n") + "\n\n" + image if image.strip() else ours
        limit = deploy.get("context_limit")
        # the image's own text as the CLI counts it, a byte more for each text it escapes (Hermit's own are taken off
        # the plan's limit: hermit.harbor.launch.context_limit)
        escaped = deploy.get("context_escaped")
        extra = image.count(escaped) if escaped and image.strip() else 0
        size = len(merged.encode("utf-8")) + extra
        if limit is not None and size > int(limit):
            raise HermitContextLimitError(
                f"hermit: {name} for {plan_label(self._plan)} would be {size} bytes (Hermit's context "
                f"{len(ours.encode('utf-8'))}, then the image's own {len(image.encode('utf-8')) + extra}), over the "
                f"CLI's limit of {limit} bytes: shorten the adapter's context")
        # A link there (an image whose AGENTS.md links its CLAUDE.md) is replaced, not written through
        await self.exec_as_root(environment, command=f"rm -f {_q(target)}")
        await self._upload_text(environment, merged, target)

    # ----- the home, the bundle, the tool script ----------------------------------------------------------------------

    async def _upload_home(self, environment: BaseEnvironment, plan: dict, workdir: str, owner: str) -> None:
        """The CLI's home as Hermit rendered it, replacing the last one: @@HERMIT_WORKDIR@@ filled in in its files
        (not in its sign-in files), all of it the agent user's and private, the sign-in files mode 600."""
        home = plan.get("home") or {}
        if not home:
            return
        target = str(home.get("container_dir") or HOME).rstrip("/")
        secrets = sorted({PurePosixPath(str(s)).as_posix() for s in home.get("secret_files") or []})
        with tempfile.TemporaryDirectory(prefix="hermit-home-") as tmp:
            staged = Path(tmp) / "home"
            _copy_tree(Path(home["host_dir"]), staged, workdir, unfilled=set(secrets))
            present = [s for s in secrets if _regular(staged / s)]
            await self.exec_as_root(environment, command=f"rm -rf {_q(target)} && "
                                                         f"mkdir -p {_q(str(PurePosixPath(target).parent))}")
            await environment.upload_dir(staged, target)
        command = f"chown -R {_q(owner)} {_q(target)} && chmod -R u+rwX,go-rwx {_q(target)}"
        if present:
            command += " && chmod 600 " + " ".join(_q(f"{target}/{s}") for s in present)
        await self.exec_as_root(environment, command=command)

    async def _upload_bundle(self, environment: BaseEnvironment, plan: dict) -> None:
        """Extra files the command needs (mini-swe-agent's: Hermit's modules), readable by everyone."""
        bundle = plan.get("bundle")
        if not bundle:
            return
        target = str(bundle["container_dir"]).rstrip("/")
        await self.exec_as_root(environment, command=f"rm -rf {_q(target)} && "
                                                     f"mkdir -p {_q(str(PurePosixPath(target).parent))}")
        await environment.upload_dir(Path(bundle["host_dir"]), target)
        await self.exec_as_root(environment, command=f"chmod -R a+rX {_q(target)}")

    async def tool_python(self, environment: BaseEnvironment) -> str:
        """The interpreter of the hermit-tool script: python3 from PATH, unless the class has its own."""
        return "/usr/bin/env python3"

    async def _install_tool_script(self, environment: BaseEnvironment, plan: dict) -> None:
        """The stdlib hermit-tool command (TOOL_SCRIPT), on PATH for the session."""
        script = TOOL_SCRIPT.replace("__PYTHON__", await self.tool_python(environment)) \
            .replace("__URL_ENV__", str(plan["tools_script"]["url_env"]))
        target = f"{TOOLS_BIN}/hermit-tool"
        await self._upload_text(environment, script, target)
        await self.exec_as_root(environment, command=f"chmod 755 {_q(target)}")

    # ----- the command ------------------------------------------------------------------------------------------------

    async def session(self, environment: BaseEnvironment, plan: dict, workdir: str) -> dict:
        """What the launch script runs: {argv, env (variables over the plan's), env_refs (a variable set from another
        one in the container), stream}. The plan's own, unless the class adjusts it (mini-swe-agent's Python); a
        mounted binary that did not serve (the class's install ran instead) is not the program: the class's program
        is, the installed CLI found on PATH."""
        argv = [self._fill(a, workdir) for a in plan["command"]]
        if self._bin and not self._mounted and PurePosixPath(argv[0]) == PurePosixPath(str(self._bin)):
            argv[0] = self.PROGRAM
        return {"argv": argv, "env": {}, "env_refs": {}, "stream": plan["stream"]}

    def run_path(self, plan: dict) -> list[str]:
        """The class's install directories, put on PATH before the plan's variables (double-quoted: $HOME is the
        agent user's own there)."""
        return list(self.RUN_PATH)

    def _path_prepend(self, plan: dict, workdir: str) -> list[str]:
        """The plan's directories for PATH; a mount whose binary did not serve (another version: the class's
        install runs instead) is left off, so that the installed CLI is the one found."""
        dirs = [self._fill(d, workdir) for d in plan.get("path_prepend") or []]
        if self._bin and not self._mounted:
            mount = str(PurePosixPath(*PurePosixPath(str(self._bin)).parts[:3]))     # /opt/hermit-<cli>
            dirs = [d for d in dirs if not (d == mount or d.startswith(mount + "/"))]
        return dirs

    @staticmethod
    def _word(arg: str) -> str:
        """One argument as a word of the launch script: quoted, the prompt's placeholder as its variable."""
        if PROMPT_MARK not in arg:
            return _q(arg)
        return f'"${PROMPT_VAR}"'.join(_q(part) if part else "" for part in arg.split(PROMPT_MARK))

    def launch_script(self, plan: dict, session: dict, workdir: str) -> str:
        """The bash script that runs the session (the module's docstring)."""
        env = {k: self._fill(v, workdir) for k, v in (plan.get("env") or {}).items()}
        env.update(session.get("env") or {})
        from_host = plan.get("env_from_host") or {}
        by_fd = plan.get("env_by_fd") or {}
        argv = session["argv"]
        lines = ["# written by harborside (hermit/harbor/harborside/hermit.py): one session of the launch plan",
                 f"cd -- {_q(workdir)} || {{ echo {_q(f'{CANNOT_ENTER} {workdir}')} >&2; exit {LAUNCH_FAILED}; }}"]
        if plan.get("nvm"):                               # with the user's own HOME: before the plan's variables
            lines.append('export NVM_DIR="$HOME/.nvm"; if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh" '
                         '>/dev/null 2>&1; fi')
        for directory in reversed(self.run_path(plan)):
            lines.append(f'export PATH="{directory}:$PATH"')
        for key, value in env.items():
            lines.append(f"export {key}={_q(value)}")
        for key, ref in (session.get("env_refs") or {}).items():
            lines.append(f'export {key}="${{{ref}-}}"')
        prepend = self._path_prepend(plan, workdir) + ([TOOLS_BIN] if plan.get("tools_script") else [])
        for directory in reversed(prepend):              # after the variables: first on PATH whatever they set
            lines.append(f'export PATH={_q(directory)}:"$PATH"')
        lines.append(f"export {HOOK_LOGS}={_q(LOGS)}")
        kept = {name for var, name in from_host.items() if var == name}
        for var, name in from_host.items():               # a secret under another name: renamed in here, by value
            if var != name:
                lines.append(f'export {var}="${{{name}-}}"')
        for name in sorted({n for v, n in from_host.items() if v != n} - kept):
            lines.append(f"unset {name}")
        for i, fd_var in enumerate(by_fd.values()):
            lines.append(f"export {fd_var}={FIRST_FD + i}")
        prompt_file = f"{RUN}/prompt.txt"
        if any(PROMPT_MARK in a for a in argv):
            lines.append(f"IFS= read -r -d '' {PROMPT_VAR} < {_q(prompt_file)} || true")
        stdin = _q(prompt_file) if plan.get("stdin") else "/dev/null"
        words = " ".join(self._word(a) for a in argv)
        stream = _q(f"{LOGS}/{session['stream']}")
        lines += ["# a process group of its own: the kill after the session reaches whatever it started",
                  "if command -v setsid >/dev/null 2>&1; then",
                  f"  setsid {words} > {stream} 2>&1 < {stdin} &",
                  "else",
                  "  set -m",
                  f"  {words} > {stream} 2>&1 < {stdin} &",
                  "fi",
                  "pid=$!",
                  f'echo "$pid" > {_q(PIDFILE)}',
                  'wait "$pid"',
                  "code=$?",
                  f"rm -f {_q(PIDFILE)}",
                  'exit "$code"']
        return "\n".join(lines) + "\n"

    def _outer_command(self, plan: dict) -> str:
        """The exec's command: the launch script, after the env_by_fd secrets went into pipes on descriptors 3, ...
        and out of the environment (`exec env -u`: the shell that had them is replaced, so neither the launch script
        nor anything it starts has them in its environment; the subshell that writes one into its pipe, a fork of that
        shell, has it there until its printf ends)."""
        script = f"{RUN}/launch.sh"
        by_fd = list((plan.get("env_by_fd") or {}).keys())
        if not by_fd:
            return f"exec bash {_q(script)}"
        pipes = " ".join(f'{FIRST_FD + i}< <(printf "%s\\n" "${name}")' for i, name in enumerate(by_fd))
        return (f"exec {pipes}; unset {' '.join(by_fd)}; exec env {' '.join(f'-u {n}' for n in by_fd)} "
                f"bash {_q(script)}")

    async def _launch(self, environment: BaseEnvironment, plan: dict, workdir: str, prompt: str, owner: str) -> None:
        with _setup_step("preparing the launch"):
            session = await self.session(environment, plan, workdir)
            self._facts["stream"] = session["stream"]
            stream = f"{LOGS}/{session['stream']}"
            await self._upload_text(environment, prompt, f"{RUN}/prompt.txt")
            await self._upload_text(environment, self.launch_script(plan, session, workdir), f"{RUN}/launch.sh")
            # The prompt and the script the agent user's, and the stream an earlier step of a stream left (collected
            # with that step) removed: the one there after the exec is this session's, made by its launch script
            await self.exec_as_root(environment, command=f"chown {_q(owner)} {_q(RUN + '/prompt.txt')} "
                                                         f"{_q(RUN + '/launch.sh')} && chmod 600 "
                                                         f"{_q(RUN + '/prompt.txt')} && "
                                                         f"chmod 700 {_q(RUN + '/launch.sh')} && rm -f {_q(stream)}")
            names = sorted({*(plan.get("env_from_host") or {}).values(), *(plan.get("env_by_fd") or {}).keys()})
            command = self._outer_command(plan)
        try:
            if names:
                result = await self._exec_by_name(environment, command, names)
            else:
                result = await environment.exec(command=command)
        except OSError as exc:                            # this machine's: the compose client not spawned (EMFILE)
            raise HermitLaunchError(f"hermit: {plan_label(plan)} did not start: its exec could not be run here: "
                                    f"{type(exc).__name__}: {exc}") from exc
        self._facts["exit_code"] = result.return_code
        if result.return_code != 0:
            said = (result.stdout or result.stderr or "").strip()[-1000:]
            if result.return_code == LAUNCH_FAILED and CANNOT_ENTER in (result.stdout or "") + (result.stderr or ""):
                raise HermitLaunchError(f"hermit: {plan_label(plan)} did not start: {said}")
            # The launch script's redirect makes the stream before the CLI starts (an earlier step's was removed): with
            # none, the CLI never started (the exec failed in Docker: runc could not start it, compose's service was not
            # running), not the agent's doing. Asked from /: Docker refuses an exec in a workdir the agent removed.
            try:
                probe = await environment.exec(command=f"test -e {_q(stream)}", cwd="/", user="root")
            except OSError as exc:                        # whether the CLI started cannot be told: not the agent's 0
                raise HermitLaunchError(f"hermit: {plan_label(plan)} exited with {result.return_code}, and its stream "
                                        f"({stream}) could not be looked for here: {type(exc).__name__}: "
                                        f"{exc}") from exc
            if probe.return_code != 0:
                raise HermitLaunchError(f"hermit: {plan_label(plan)} did not start: its exec exited with "
                                        f"{result.return_code} and left no stream ({stream})" +
                                        (f"\n{said}" if said else ""))
            raise NonZeroAgentExitCodeError(f"hermit: {plan_label(plan)} exited with {result.return_code} (its stream: "
                                            f"{stream})" + (f"\n{said}" if said else ""))

    async def _exec_by_name(self, environment: BaseEnvironment, command: str, names: list[str]):
        """The command as Harbor's exec runs it (the task's workdir, its user, the merged variables), with the
        variables `names` passed to `docker compose exec` by name: their values come from this process's environment
        (the compose client reads them) and never reach a command line."""
        if not all(callable(getattr(environment, n, None)) for n in COMPOSE_EXEC) or \
                getattr(environment, "_is_windows_container", False):
            raise HermitLaunchError(f"hermit: the session's secrets ({', '.join(names)}) go to `docker compose exec` "
                                    "by name, which only a local Docker environment does: run the trial in Harbor's "
                                    "docker environment")
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            raise HermitLaunchError(f"hermit: {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in "
                                    "Harbor's environment, and the launch plan passes it to the session by name: set "
                                    "it where Hermit's runner starts harbor")
        argv = ["exec"]
        workdir = getattr(getattr(environment, "task_env_config", None), "workdir", None)
        if workdir:
            argv += ["-w", str(workdir)]
        for key, value in (environment._merge_env(None) or {}).items():
            if key not in names:
                argv += ["-e", f"{key}={value}"]
        for name in names:
            argv += ["-e", name]
        user = environment._resolve_user(None)
        if user is not None:
            argv += ["-u", str(user)]
        argv += ["main", "bash", "-c", command]
        return await environment._run_docker_compose_command(argv, check=False,
                                                             on_output=environment._output_callback())

    # ----- after the session ------------------------------------------------------------------------------------------

    async def _after_session(self, environment: BaseEnvironment, plan: dict, workdir: str, *,
                             cancelled: bool = False, failed: bool = False) -> None:
        """The kill, the copy-back, the collected files and the returned ones, each on its own and with its execs
        from / (Docker refuses an exec in a workdir the session removed): a failure is logged and recorded
        (after_session_errors), never raised over the session's own outcome. Harbor's timeout landing in one of them
        is recorded as its cancellation, the steps after it still run, and the cancellation goes on after the last
        one, unless the session raised an error of its own (`failed`: a launch failure, a non-zero exit), which then
        goes on as it is (Harbor's wait_for turns only a cancellation into the agent's timeout, so that error is not
        taken for one). The return of the files is the one step run once more when the timeout landed in it (a refreshed
        sign-in still comes back): its copy of a file can overlap the cancelled attempt's (a `docker compose cp` whose
        wait was cancelled goes on running on this host), the same file written twice, while the copy-back's and the
        collect's `rm -rf` then `cp -a` could nest a copy inside the other. Once the run is cancelled (there, or earlier
        in `run`: `cancelled`), each step left (the return run once more included) has AFTER_CANCEL_S at most (a hung
        container would hold every one), and a second cancellation (the job's, after Harbor's timeout) goes on at once,
        the steps after it not run."""
        caught: asyncio.CancelledError | None = None
        steps = [self._stop_session, self._copy_back, self._collect, self._return_files]
        while steps:
            step = steps.pop(0)
            name = step.__name__.strip("_")
            work = step(environment, plan, workdir)
            try:
                await (_bounded(work) if cancelled else work)
            except asyncio.CancelledError as exc:
                if cancelled:
                    raise                                  # the second: no step waits any longer
                self.logger.warning("hermit: %s after the session was cancelled", name)
                self._facts.setdefault("after_session_errors", []).append(f"{name}: cancelled")
                caught, cancelled = exc, True
                if step == self._return_files:             # the sign-in's return never finished: once more, bounded
                    steps.insert(0, step)
            except Exception as exc:                       # noqa: BLE001 - the session's own error, if any, goes on
                self.logger.warning("hermit: %s after the session failed: %s", name, exc)
                self._facts.setdefault("after_session_errors", []).append(f"{name}: {exc}")
        if caught is not None and not failed:             # a session that failed: its own error goes on instead
            raise caught

    async def _stop_session(self, environment: BaseEnvironment, plan: dict, workdir: str) -> None:
        """A session still running (Harbor's timeout cancelled `run`, or the launch failed around it) is killed with
        its process group: TERM, up to the plan's grace for it to exit, then KILL. One that ended by itself has no
        pid file left, and what it started in the background goes on."""
        grace = int(plan.get("kill_grace_s", DEFAULT_KILL_GRACE_S))
        alive = '{ kill -0 "$p" 2>/dev/null && ! grep -qs "^State:[[:space:]]*Z" /proc/"$p"/status; }'
        command = (f'p=$(cat {_q(PIDFILE)} 2>/dev/null); if [ -n "$p" ] && {alive}; then '
                   f'kill -TERM -- -"$p" 2>/dev/null; i=0; while [ "$i" -lt {grace} ] && {alive}; do sleep 1; '
                   f'i=$((i+1)); done; kill -KILL -- -"$p" 2>/dev/null; echo hermit-killed; fi; '
                   f'rm -f {_q(PIDFILE)}; true')
        result = await environment.exec(command=command, cwd="/", user="root", timeout_sec=grace + 30)
        if "hermit-killed" in (result.stdout or ""):
            self._facts["killed"] = True

    async def _copy_back(self, environment: BaseEnvironment, plan: dict, workdir: str) -> None:
        """The plan's copy_back directories (relative to the workdir) to /logs/agent/hermit-copy/<basename>: what the
        agent wrote that Hermit keeps (an adapter's memory). A link there is not followed. A copy that failed (a full
        disk, a read error) is removed, never kept in part (Hermit's side would take it for the whole: a cut memory
        merged into the adapter's), and fails the step (its exec exits 1), after the other copies and the chmod that
        keeps what was copied readable."""
        dirs = (plan.get("deploy") or {}).get("copy_back") or []
        if not dirs:
            return
        root = workdir.rstrip("/")
        parts = [f"mkdir -p {_q(COPY_DIR)}", "f=$?"]          # f: 1 once a part failed; every part still runs
        for rel in dirs:
            source, target = _q(f"{root}/{rel}"), _q(f"{COPY_DIR}/{PurePosixPath(rel).name}")
            parts.append(f"rm -rf {target} || f=1; if [ -d {source} ] && [ ! -L {source} ]; then "
                         f"cp -a {source} {target} || {{ f=1; rm -rf {target}; }}; fi")
        parts += [f"chmod -R a+rX {_q(COPY_DIR)} || f=1", 'exit "$f"']
        await self.exec_as_root(environment, command="; ".join(parts), cwd="/")

    async def _collect(self, environment: BaseEnvironment, plan: dict, workdir: str) -> None:
        """The plan's collect files (a file or a directory) to /logs/agent/hermit-collect/<to>; never the home's
        sign-in files, and never through a link. A copy that failed (a full disk, a read error) is removed, never kept
        in part (Hermit's side would take it for the whole: a cut last message for the reply, where without the file it
        reads the stream's), and fails the step (its exec exits 1), after the other copies and the chmod that keeps
        what was copied readable."""
        entries = plan.get("collect") or []
        if not entries:
            return
        home = plan.get("home") or {}
        target_home = str(home.get("container_dir") or HOME).rstrip("/")
        secret = {f"{target_home}/{PurePosixPath(str(s)).as_posix()}" for s in home.get("secret_files") or []}
        parts = [f"mkdir -p {_q(COLLECT_DIR)}", "f=$?"]       # f: 1 once a part failed; every part still runs
        for entry in entries:
            source = self._fill(entry["from"], workdir)
            if str(PurePosixPath(source)) in secret:
                self.logger.warning("hermit: %s is a sign-in file of the home: it is not collected into %s", source,
                                    LOGS)
                continue
            target = f"{COLLECT_DIR}/{PurePosixPath(entry['to']).as_posix()}"
            s, t = _q(source), _q(target)
            failed = f"{{ f=1; rm -rf {t}; }}"                # a copy that failed is removed, never kept in part
            parts.append(f"mkdir -p {_q(str(PurePosixPath(target).parent))} || f=1; rm -rf {t} || f=1; "
                         f"if [ -L {s} ]; then :; elif [ -d {s} ]; then cp -a {s} {t} || {failed}; "
                         f"elif [ -f {s} ]; then cp {s} {t} || {failed}; fi")
        parts += [f"chmod -R a+rX {_q(COLLECT_DIR)} || f=1", 'exit "$f"']
        await self.exec_as_root(environment, command="; ".join(parts), cwd="/")

    async def _return_files(self, environment: BaseEnvironment, plan: dict, workdir: str) -> None:
        """The plan's return_files from the container to their host paths (Hermit's side takes them from there: a
        refreshed ChatGPT sign-in), never into the logs. One the session did not leave (or left as a link) is skipped;
        one whose check could not tell (its exec failed: compose's service stopped) raises, never taken for none."""
        for entry in plan.get("return_files") or []:
            source = self._fill(entry["from"], workdir)
            s = _q(source)
            check = await environment.exec(command=f'test -f {s} && test ! -L {s} && echo "{RETURN_LINE}yes" || '
                                                   f'echo "{RETURN_LINE}no"', cwd="/", user="root")
            there = marked(check.stdout, RETURN_LINE) if check.return_code == 0 else None
            if there == "no":
                continue
            if there != "yes":
                raise RuntimeError(f"whether the session left {source} could not be told (exit {check.return_code}): "
                                   f"{(check.stdout or check.stderr or '').strip()[-300:]}")
            target = Path(entry["to"])
            target.parent.mkdir(parents=True, exist_ok=True)
            await environment.download_file(source, target)

    # ----- what Harbor records ----------------------------------------------------------------------------------------

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Metadata only: Hermit reads the session from its stream, not Harbor."""
        plan = self._plan or {}
        deploy = plan.get("deploy") or {}
        facts = {"hermit_cli": self.CLI, "adapter_deployed": bool(deploy), "adapter_id": deploy.get("adapter_id"),
                 "hooks_active": self._hooks_active, "python3": self._python3_ok, "mounted": self._mounted,
                 "exit_code": self._facts.get("exit_code"), "killed": bool(self._facts.get("killed")),
                 "stream": self._facts.get("stream") or plan.get("stream")}
        if self._facts.get("after_session_errors"):
            facts["after_session_errors"] = list(self._facts["after_session_errors"])
        context.metadata = {**(context.metadata or {}), **(plan.get("metadata") or {}), **facts}


def prompt_of(plan: dict, instruction: str) -> str:
    """The session's prompt: Harbor's instruction, then the plan's prompt_suffix; the plan's prompt_slash_prefix before
    one that starts with `/` (which Claude Code would otherwise run as one of its commands)."""
    prompt = instruction + str(plan.get("prompt_suffix") or "")
    prefix = plan.get("prompt_slash_prefix")
    return prefix + prompt if prefix and prompt.startswith("/") else prompt


def plan_label(plan: dict | None) -> str:
    """The CLI's name in messages."""
    plan = plan or {}
    return str(plan.get("label") or plan.get("cli") or "the CLI")
