"""`codex`, `claude`, `pi` and `dsh`: a coding-agent CLI as a Base Agent
(docs/framework/agent-layers.md#layer-2-base-agent).

One `run` is one session of the CLI in print mode: the prompt is given, the CLI works in `workspace` with its own
tools and the call's `tools` (served over MCP by the framework's tool server), and its last message is the reply.
The CLI's event stream is kept unchanged (`Result.log`) and read into the trajectory and the usage.

`CliAgent` runs every CLI the same way (isolation, home, sign-in, tool server, process, the stream's safety); what
differs per CLI is its subclass's hooks: its command, its home (`home_env`, `prepare_home`, `user_home`), how its
stream is read (`served`, `notices`, `last_message`, `parse`, `steps`, `adjust_usage`), its web search, its system
instructions and its isolations. `CLIS` names every CLI; a new one is a subclass there, its stream's reader in
hermit.agent.base.trajectory, and its model binding in hermit.agent.model.backends.

Settings (given once; `configured(...)` for a call's own):
  model             the CLI's model (absent: its default); with `binding`, the provider settings are in `args`/`env`
  binding           the resolved `<backend>/<model>` reference (hermit.agent.model.backends), for the key and price
  login             chatgpt: Codex with the user's ChatGPT sign-in (hermit.agent.base.login), from the copy kept
                    under `login_root` (default: the shared one); claude: Claude Code on the user's subscription, with
                    the token in CLAUDE_CODE_OAUTH_TOKEN and nothing else that could pay (see CLAUDE_BILLING). Sessions
                    on a login wait for its plan
  plan_dir          the plan's directory the sessions on a login share (default: the machine's one per login,
                    login.PLAN or login.CLAUDE_PLAN)
  plan_max_wait_s   how long a session on a login waits for the plan before it is unavailable (default 12 h;
                    None: no limit)
  deadline          a time (time.time()) past which no session starts and none runs on: the plan's waits and a
                    refused session's rerun come off its timeout (a caller stopped at that time: a container session's)
  effort            Codex's model_reasoning_effort
  web_search        the CLI's web search, on or off (required: set either way)
  isolation         none: the CLI on this machine; sandbox: inside `sandbox` (the run's hermit.sandbox.Sandbox);
                    container: in a Docker container of its own (`container`: image, network, proxy;
                    hermit.agent.base.container), where the CLI's own sandbox holds the session's commands (Codex's
                    `codex_sandbox`; Claude Code's CLAUDE_SANDBOX)
  codex_sandbox     Codex's own sandbox mode (default workspace-write)
  permission_mode   Claude Code's (default bypassPermissions)
  shell_env         Codex's shell_environment_policy.inherit for the session's commands (default core: no keys)
  project_doc_max_bytes   Codex's cap on AGENTS.md
  home              the CLI's home (CODEX_HOME, CLAUDE_CONFIG_DIR); kept between calls, so a session can be resumed.
                    Absent: a fresh directory per call, under $HERMIT_HOMES (default ~/.cache/hermit/homes; not the
                    temporary directory, where Codex makes no helper programs for its sandbox and no command of the
                    session runs)
  home_files        files of the user's CLI home copied into `home` for the call, when there is no login and no
                    binding (default: auth.json for Codex, .credentials.json for Claude Code); removed after it
  log               the event stream's path (default: agent-<backend>.jsonl in `home`)
  args, env         extra CLI arguments, and environment variables (a `$NAME` value is expanded)
  price             USD per million tokens, for a CLI that reports no cost
  timeout_s         the session's wall clock (default 3600); Limits.timeout_s overrides it
  cancel            a threading.Event: the session is stopped once it is set
  tools_name        the MCP server's name for the call's tools (default hermit_tools)
  bin               the CLI's program (default codex / claude)
  cwd               Codex: the working directory it is told, when the isolation shows the workspace under another
                    path (MWM's /w)
  tools_env         a variable that gets the tools' JSON entrance (GET <url>/tools, POST <url>/call/<name>)
  mcp_tools         the tools offered over MCP (default: all of them; the JSON entrance has all)
  stdin             true: the prompt is given on stdin, not on the command line (a long one)
  stderr_log        the CLI's stderr in a file of its own (default: in the event stream, after stdout's lines)
  writable          more paths the session may write inside the run's sandbox (besides its home)
  env_drop          variables of this process's environment the session does not get
  log_dir           a chat endpoint over this agent (`llm: {cli: ...}`) keeps each call's event stream here
  plan_rerun        on a login, a session the plan refused before it finished a turn is run again in the same
                    workspace once the plan allows (default true); false: it is returned (`extra["refused"]`), for a
                    caller that would rather start over from its own files
  text_only         a session that can only reply (a model behind a chat endpoint, a role that answers from its
                    prompt). Claude Code: `--restricted`, no tools, no MCP servers, no commands, no settings or
                    memory files, `@` mentions left as text, and a session that reports otherwise has failed
  Claude Code only: output_format (stream-json, or json: one result object), allowed_tools (its --tools; empty: none),
  persist_session (false: --no-session-persistence), system_prompt (in place of its own; written to a file in the
  home), append_system_prompt (a long one goes the same way), effort, permission_mode, usage_credits (true: sessions on
  `login: claude` may draw the account's usage credits; default: a session that did holds the next ones back)
  Pi only: provider (its models.json entry for a bound model, from the binding: Binding.pi_provider),
  image_input (true: the bound model takes images, such as a tool's result; default: text only, Pi's default for a
  model it does not know), append_system_prompt, effort (its --thinking level). Pi takes no roles, has no web search
  and runs under isolation none or sandbox.
  DeepSeek Harness (dsh) only: provider (its pi-ai route for a bound model, from the binding: Binding.dsh_provider),
  image_input (as Pi's), effort (the default model's reasoningEffort; YAML's bare `off`, a boolean, is `off`),
  permission_mode (its sandbox: read-only, workspace-write, the default, or danger-full-access; under workspace-write
  the `writable` paths are granted to it with --add-dir), instruction_files (false: no AGENTS.md or CLAUDE.md of the
  working directory or below it is given to the model; default true). dsh takes no roles here and runs under
  isolation none or sandbox; `args` may hold its own options.

Limits are enforced by the isolation, or the call is refused (Unsupported):

  isolation   readable                          hidden                         network
  none        linked into the workspace          refused                        refused (any list)
  sandbox     shown at its target (a bind)       refused when the sandbox shows it    the sandbox's own: [] only
  container   bound read-only at their target    all but workspace, home, data  the container's proxy: [] only
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Sequence
import urllib.parse

from hermit.agent.base import container, login as codex_login
from hermit.agent.base.interface import BaseAgent, Limits, Mount, Result, Unsupported
from hermit.agent.base.records import parse_claude, parse_codex, parse_dsh, parse_pi, price_usage
from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import claude_steps, codex_steps, dsh_steps, events, pi_steps
from hermit.agent.model.backends import CLAUDE_ENV

ISOLATIONS = ("none", "sandbox", "container")
# The CLI's own reports of a provider it could not reach (it retries those itself first), and of a container that
# could not start, not the session's text
UNAVAILABLE = re.compile(r"unexpected status (?:429|5\d\d)|exceeded retry limit|stream disconnected|"
                         r"error sending request|too many requests|service unavailable|bad gateway|overloaded|"
                         r"model is at capacity|"
                         r"^(?:429|5\d\d): |"                     # Pi's report of a provider's status
                         # dsh's, by its codes: those it retries itself, and a stream the provider closed
                         r"^dsh: (?:RATE_LIMIT|SERVER|TIMEOUT|TRANSPORT|EMPTY_RESPONSE|STREAM_CLOSED): |"
                         r"error response from daemon|cannot connect to the docker daemon|"
                         + re.escape(container.MISSING), re.I | re.M)
CODEX_HOME_SETTINGS = ["features.shell_snapshot = false",          # a snapshot would bring back the whole environment
                       "sandbox_workspace_write.network_access = false", 'approval_policy = "never"']
# What would send a session on `login: claude` to another bill or host. The CLI takes any of these over the token, and
# in print mode without asking. They are dropped from the session's environment; a workspace's or a session's own
# settings file can set them too (a hook, a command, an adapter's `.claude/settings.json`), and `--settings` outranks
# those (CLAUDE_LOCK), at the end of the command line, where a later `--settings` would replace it.
CLAUDE_BILLING = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
                  "ANTHROPIC_UNIX_SOCKET", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
                  "ANTHROPIC_ORGANIZATION_ID", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                  "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_USE_MANTLE",
                  "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD")
CLAUDE_LOCK = json.dumps({"apiKeyHelper": "", "env": {name: "" for name in CLAUDE_BILLING}})
# Claude Code's own sandbox for a working session in a container, as Codex's workspace-write sandbox runs in its: the
# session's commands (its Bash tool) run in bubblewrap, in a process namespace of their own (the CLI's process and its
# environment out of sight), with no network (no host allowed, and none asked for: `strictAllowlist`), writing only the
# workspace and /tmp; a command it cannot sandbox is not run, and none may be run outside it. In the last `--settings`,
# which Claude Code lets no project or local settings file widen (their sandbox hosts, paths and switches are ignored).
CLAUDE_SANDBOX = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                  "network": {"allowedDomains": [], "strictAllowlist": True}, "filesystem": {"allowWrite": ["/tmp"]}}
# ...and the session's own file tools (Write, Edit), which run in the CLI's process, outside that sandbox, may not change
# the workspace's Claude Code settings: Claude Code reloads them mid-session, and a hook the session wrote there would
# run on its next tool call, outside the sandbox. A deny rule holds under bypassPermissions too. (What a deployment
# puts there runs as deployed; the image's CLI is undumpable, so that cannot read the CLI's memory: docker/claude/.)
CLAUDE_OWN_SETTINGS = {"deny": ["Edit(.claude/**)"]}
CLAUDE_TOKEN_FD = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"     # where Claude Code reads its token from a descriptor
# Not this session's: another model's settings, and a parent Claude Code's own (Hermit started from one of its sessions).
CLAUDE_INHERITED = (*CLAUDE_ENV, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_EFFORT",
                    "CLAUDE_PID", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
# A session that only replies: nothing it was not given reaches the model (documented in the CLI's env-vars reference).
CLAUDE_TEXT_ENV = {"CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",     # `@path` in a prompt stays text, no file is read
                   "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
# The same model and bill for the whole session: the CLI otherwise moves to another model when a classifier flags a
# request or a model is out of reach. No update check or telemetry either.
CLAUDE_LOGIN_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
                    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1"}
# The CLI's own kinds of failure (a failed `assistant` event's `error`), and the wordings of a stream without them
# (`--output-format json`, an older CLI). "(not your usage limit)" is the CLI's text for a throttled request.
CLAUDE_LOGIN_LOST = ("authentication_failed", "oauth_org_not_allowed", "account_on_hold", "verification_required",
                     "billing_error", "cloud_credential_error")
CLAUDE_BUSY = ("rate_limit", "overloaded", "server_error")
CLAUDE_REJECTED = ("invalid_request", "model_not_found", "max_output_tokens")
CLAUDE_LIMIT = re.compile(r"hit your [^.\n]*limit|out of usage|usage_limit_reached", re.I)
CLAUDE_AUTH = re.compile(r"not logged in|failed to authenticate|login expired|please run /login|"
                         r"credit balance is too low|does not have access to claude", re.I)
CLAUDE_DOWN = re.compile(r"not your usage limit|api error: (?:429|5\d\d)|request timed out|connection (?:error|refused|"
                         r"to the api was lost)|no response from api|stream idle timeout|high load|overloaded", re.I)
FINISH = {"max_tokens": "length", "pause_turn": "length", "refusal": "content_filter"}
_ready: set = set()


def _of(backend: str) -> type["CliAgent"]:
    """The CLI a module-level helper's `backend` names: Codex's, else Claude Code's (as these helpers always read)."""
    return CLIS.get(backend, Claude)


# Claude Code's own tools whose work outlives the session or reaches past it: a routine on the user's account
# (RemoteTrigger), prompts scheduled for later (CronCreate, ScheduleWakeup), and messages to the user's other Claude
# sessions (ListAgents, SendMessage). A session Hermit starts is one task or one learner's turn, and a learned skill or
# hook could otherwise leave work running on the plan after the run, or reach a session of the user's: they are denied
# in every session.
CLAUDE_BEYOND_SESSION = ("RemoteTrigger", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "ListAgents",
                         "SendMessage")


def web_search_args(backend: str, enabled: bool) -> list[str]:
    """A CLI session's web search, set explicitly either way (CliAgent.web_search_args)."""
    return _of(backend).web_search_args(enabled)


def _regular(path) -> bool:
    """Whether `path` is a regular file (never a link, a FIFO or a directory: lstat, not stat)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _clear(path) -> None:
    """Before the Base Agent writes `path` in a place a session could write (its home, its log's directory):
    anything there but a regular file (a link, a FIFO, a directory) is removed, so the write lands in a file of its
    own and never through a link or into a pipe that blocks."""
    if os.path.lexists(path) and not _regular(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def _in_places(path, places) -> bool:
    """Whether `path` lies in one of `places` (under its given or its real name)."""
    given = os.path.abspath(str(path))
    names = {given, os.path.join(os.path.realpath(os.path.dirname(given)), os.path.basename(given))}
    roots = {n for p in places for n in (os.path.abspath(str(p)), os.path.realpath(str(p)))}
    return any(n == r or n.startswith(r.rstrip("/") + "/") for n in names for r in roots)


def _session_output(path, places) -> None:
    """Before this process opens `path` for a session's output (its event stream, its stderr). In a place the session
    can write (`places`: its home, its workspace, the writable paths) a link on the way below the place is removed
    and real directories made, and anything at the path but a regular file is removed (`_clear`): a link would be
    written through, a pipe would block, a directory would stop the write. A path elsewhere is the caller's own (a
    log file, `/dev/stderr`, a collector's pipe) and is opened as it is."""
    given = os.path.abspath(str(path))
    roots = [os.path.realpath(str(p)) for p in places]
    parts = Path(given).parts
    # The first directory on the way, from the top, that lies in a place (under whatever name the path spells it):
    # nothing above it is the session's, so resolving it follows no link the session left; below it, nothing is
    # followed.
    for i in range(1, len(parts)):
        real = os.path.realpath(os.path.join(*parts[:i]))
        if any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
            current = os.path.join(*parts[:i])
            for part in parts[i:-1]:
                current = os.path.join(current, part)
                if os.path.islink(current) or (os.path.lexists(current) and not os.path.isdir(current)):
                    os.unlink(current)                  # never followed: the session could have left it
                os.makedirs(current, exist_ok=True)
            break
    if _in_places(path, places):
        _clear(path)


def _files(root: Path) -> list[Path]:
    """Every file under `root`, no link followed."""
    return [Path(directory) / name for directory, _, names in os.walk(root) for name in names]


def _scrub_token(paths) -> None:
    """The login's token, wherever it is printed in `paths` (regular files, not followed through a link), replaced by
    its name."""
    token = os.environ.get(codex_login.TOKEN_ENV, "")
    if len(token) < 20:
        return
    secret, name = token.encode(), f"<{codex_login.TOKEN_ENV}>".encode()
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            with os.fdopen(os.dup(fd), "rb") as f:
                data = f.read()
            if secret in data:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                view = memoryview(data.replace(secret, name))
                while view:
                    view = view[os.write(fd, view):]
        except OSError:
            continue
        finally:
            os.close(fd)


def _identity(path):
    """The regular file at `path` (followed: a caller's path may be a link), as (device, inode), or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISREG(st.st_mode) else None


def _restore_home(home: Path) -> None:
    """`home` (resolved before the call, so no link on its way) a real directory again after the call: a link that
    appeared on the way (the session could write its home, or a place above it) is removed, never followed."""
    if os.path.realpath(home) == str(home) and os.path.isdir(home):
        return
    current = Path(home.anchor)
    for part in home.parts[1:]:
        current = current / part
        if os.path.islink(current) or (os.path.lexists(current) and not os.path.isdir(current)):
            os.unlink(current)
        if not os.path.lexists(current):
            current.mkdir()


def _real_dir(path: Path) -> Path:
    """`path` as a real directory: a link or a file left there is removed first."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isdir(path)):
        os.unlink(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_home(home: Path, settings: dict, roles: Sequence[dict] = (), tools_url: str | None = None,
               tools_name: str = "hermit_tools") -> None:
    """Codex's home config.toml: no shell snapshot, the session's commands without network and with only the
    variables `shell_env` lets through (core: no key, no proxy), no approvals, the hosted web search as `web_search`
    sets it, the roles, Codex's AGENTS.md cap when one is given, with a login the ChatGPT sign-in, and the call's tools
    as an MCP server whose calls need no approval. The provider settings of a bound backend are in `args`."""
    import tomllib
    lines = list(CODEX_HOME_SETTINGS)
    if settings.get("project_doc_max_bytes"):
        lines.append(f"project_doc_max_bytes = {int(settings['project_doc_max_bytes'])}")
    lines.append(f'web_search = "{'live' if settings.get('web_search') else 'disabled'}"')   # set either way
    if settings.get("login") == "chatgpt":
        lines += ['forced_login_method = "chatgpt"', *codex_login.config_lines()]
    if roles:
        lines.append("features.multi_agent = true")
    lines += ["", "[shell_environment_policy]", f'inherit = "{settings.get("shell_env") or "core"}"',
              'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]']
    if tools_url:
        lines += ["", f"[mcp_servers.{tools_name}]", f"url = {json.dumps(tools_url)}",
                  'default_tools_approval_mode = "approve"', "tool_timeout_sec = 1800"]
    _real_dir(home / "agents")
    for role in roles:
        spec = "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in
                         {"name": role["name"], "description": role["description"],
                          "developer_instructions": role["instructions"]}.items()) + "\n"
        tomllib.loads(spec)
        _clear(home / "agents" / f"{role['name']}.toml")
        (home / "agents" / f"{role['name']}.toml").write_text(spec, encoding="utf-8")
        lines += ["", f"[agents.{role['name']}]", f"description = {json.dumps(role['description'], ensure_ascii=False)}",
                  f"config_file = {json.dumps('agents/' + role['name'] + '.toml')}"]
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    _clear(home / "config.toml")
    (home / "config.toml").write_text(text, encoding="utf-8")


def last_message(backend: str, log: Path, last: Path | None = None) -> str:
    """The session's last message (CliAgent.last_message)."""
    return _of(backend).last_message(log, last)


def _objects(raw: str):
    """(the JSON objects of a stream, the lines that are none): a CLI's events, and its own messages beside them."""
    objects, other = [], []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            objects.append(event)
        elif line.strip():
            other.append(line)
    return objects, other


def _last_result(objects: list[dict]) -> dict | None:
    """Claude Code's last result event (a session whose sub-agents run in the background has several)."""
    results = [e for e in objects if e.get("type") == "result"]
    return results[-1] if results else None


def _succeeded(result: dict | None) -> bool:
    """Whether a Claude Code result says the model answered. A failure ends with a result too, its exit code 1, often
    of subtype `success` with `is_error` set and the failure's text as `result`; a prompt the CLI took as one of its own
    commands ends with a result of no turn, the command's output for text."""
    return bool(result) and not result.get("is_error") and str(result.get("subtype") or "success") == "success" \
        and result.get("num_turns", 1) != 0


def served(backend: str, raw: str) -> bool:
    """Whether the provider served the session (CliAgent.served)."""
    return _of(backend).served(raw)


def notices(backend: str, raw: str) -> str:
    """What the CLI itself reported, apart from the session's own work (CliAgent.notices)."""
    return _of(backend).notices(raw)


ARG_MAX = 120_000            # bytes of one command-line argument to stay under (Linux allows 128 KiB per argument)
EFFORTS = {"max": "xhigh"}   # an effort another CLI names (Claude Code's --effort) as Codex's model_reasoning_effort


def codex_instructions(system: str, prompt: str) -> tuple[list[str], str]:
    """Codex's developer instructions for a session: (its -c arguments, the prompt). Instructions too long for one
    argument go at the top of the prompt instead, as a marked block the short instruction points at."""
    if not system:
        return [], prompt
    arg = f"developer_instructions={json.dumps(system)}"
    if len(arg.encode()) <= ARG_MAX:
        return ["-c", arg], prompt
    pointer = "Follow the SYSTEM INSTRUCTIONS block at the top of the message."
    return (["-c", f"developer_instructions={json.dumps(pointer)}"],
            f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{prompt}")


def run_process(command: list[str], *, env: dict, cwd: Path, stream: Path, timeout: float | None,
                cancel=None, stdin: str | None = None, stderr: Path | None = None) -> tuple[int, bool, bool]:
    """A process group with stdout to `stream` and stderr there too, or to `stderr`; (exit code, timed out,
    cancelled). Its stdin is `stdin`, written by a thread of its own (a CLI that does not read it cannot hold the
    clock up) and then closed, or empty: a CLI given a prompt still reads a non-terminal stdin to its end as more
    prompt, and an inherited one (a shell's socket, a pipe that never closes) would hang the session."""
    deadline = time.time() + timeout if timeout else None
    with open(stream, "w") as out, open(stderr or os.devnull, "w") as err:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE, stdout=out,
                                stderr=err if stderr else subprocess.STDOUT, env=env, cwd=str(cwd),
                                start_new_session=True, text=True)
        if stdin is not None:
            def feed():
                try:
                    proc.stdin.write(stdin)
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:                  # closed however the write went: the CLI reads its stdin to the end
                    try:
                        proc.stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
            threading.Thread(target=feed, daemon=True).start()
        timed_out = cancelled = False
        try:
            while proc.poll() is None:
                timed_out = deadline is not None and time.time() > deadline
                cancelled = cancel is not None and cancel.is_set()
                if timed_out or cancelled:
                    _stop(proc)
                    break
                time.sleep(0.2)
        except BaseException:                  # the caller was interrupted: the session's own group does not hear it
            _stop(proc)
            raise
        code = proc.wait()
    return (-1 if timed_out else code), timed_out, cancelled


def _stop(proc) -> None:
    """The process, its group and every process below it: a sandbox starts its program in a session of its own,
    which a group kill does not reach."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for target in _descendants(proc.pid):
            try:
                os.kill(target, sig)
            except ProcessLookupError:
                pass
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(15)
            return
        except subprocess.TimeoutExpired:
            continue


def _descendants(pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = entry.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        children.setdefault(int(fields[1]), []).append(int(entry.parent.name))
    out, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


class CliAgent(BaseAgent):
    """One CLI's session as a Base Agent: the shared run, and the hooks each CLI's subclass fills in."""
    backend = ""                                  # the CLI's name (make, CLIS, its stream agent-<backend>.jsonl)
    label = ""                                    # its name in messages
    program = ""                                  # its program, unless the `bin` setting names another
    aliases: tuple[str, ...] = ()                 # other names make() takes for it
    isolations: tuple[str, ...] = ("none", "sandbox")   # of ISOLATIONS, the ones it runs under
    home_var = ""                                 # the variable that points the CLI at its home
    user_folder = ""                              # the user's own home of the CLI, under ~
    home_files: tuple[str, ...] = ()              # files of the user's home a session's home gets (the sign-in first)
    turn_done = ""                                # the event type of a turn the provider served

    # ---------- the CLI's own: its command and its home ----------
    def command(self, prompt: str, workspace: Path, last_file: Path, *, session=None, roles=(), tools_url=None) -> list:
        raise NotImplementedError

    def home_env(self, home: Path) -> dict[str, str]:
        """The variables that point the CLI at the session's home."""
        return {self.home_var: str(home)}

    def prepare_home(self, home: Path, roles: Sequence[dict], tools_url: str | None, tools_name: str) -> None:
        """The session's configuration in its home, before the call (nothing, for a CLI given it on its command
        line)."""

    @classmethod
    def user_home(cls) -> Path:
        """The user's own home of the CLI, where `home_files` are copied from."""
        return Path.home() / cls.user_folder

    @classmethod
    def web_search_args(cls, enabled: bool) -> list[str]:
        """The CLI's arguments that set its web search, on or off."""
        raise NotImplementedError

    def system_settings(self, system: str, prompt: str) -> tuple[dict, str]:
        """System instructions for one call: (settings for `configured`, the prompt)."""
        raise NotImplementedError

    # ---------- the CLI's own: reading its stream ----------
    @classmethod
    def served(cls, raw: str) -> bool:
        """Whether the provider served the session: it finished a turn (an event of that type, not the words)."""
        return any(e.get("type") == cls.turn_done for e in _objects(raw)[0])

    @classmethod
    def notices(cls, raw: str) -> str:
        """What the CLI itself reported, apart from the session's own work: its lines that are no event (its stderr),
        and its error events (`errors`). A message, a command's output or a file the session wrote is not among
        them, so a session that read the words of an outage or of a usage limit (a log it grepped) is not taken for
        one that met it."""
        objects, other = _objects(raw)
        found = list(other)
        for e in objects:
            found += cls.errors(e)
        return "\n".join(found)

    @classmethod
    def errors(cls, event: dict) -> list[str]:
        """The error texts the CLI reported in one event."""
        return []

    @classmethod
    def last_message(cls, log: Path, last: Path | None = None) -> str:
        """The session's last message, from its stream (or the file `last` the CLI wrote it to)."""
        raise NotImplementedError

    @classmethod
    def parse(cls, log: Path, model: str):
        """(session handle, usage) from the stream (hermit.agent.base.records)."""
        raise NotImplementedError

    @classmethod
    def steps(cls, stream: list[dict]) -> list:
        """The stream's events as trajectory steps (hermit.agent.base.trajectory)."""
        raise NotImplementedError

    def adjust_usage(self, home: Path, handle, usage: list, resumed: bool) -> list:
        """This call's usage from what the stream reported (unchanged, for a CLI that reports each call's own)."""
        return usage

    # Without isolation, a mount inside the workspace is a link to its source (`_link`); a CLI whose searches do not
    # follow links (Claude Code's Glob and Grep and dsh's glob and grep run ripgrep without --follow; `grep -r` and
    # `find` in a shell neither) gets it bound there instead, a real directory, as a container gives it (`_bound`).
    binds_mounts = False

    # ---------- what the call may do ----------
    def _isolation(self, workspace: Path, limits: Limits):
        """The isolation and its settings: (name, container settings or None, sandbox or None). Refuses limits the
        isolation cannot keep."""
        s = self.settings
        isolation = s.get("isolation") or "none"
        if isolation not in ISOLATIONS:
            raise ValueError(f"isolation must be one of {ISOLATIONS}, not {isolation!r}")
        if isolation not in self.isolations:
            why = "container runs Codex" if isolation == "container" else f"{isolation} is not for {self.label}"
            raise Unsupported(f"isolation: {why}; {self.label} takes isolation {' or '.join(self.isolations)}")
        network = None if limits.network is None else list(limits.network)
        if isolation == "none":
            if limits.hidden:
                raise Unsupported(f"{self.name} without isolation cannot hide paths: use isolation sandbox or container")
            if network is not None:
                raise Unsupported(f"{self.name} without isolation cannot limit the network: use isolation sandbox or "
                                  "container")
            for m in limits.readable:                 # linked into the workspace: nothing is left outside it
                target = _target(m, workspace)
                placed = target.parent.resolve() / target.name     # `..` and linked folders on the way resolved
                if target.resolve() != Path(m.source).resolve() and not placed.is_relative_to(workspace):
                    raise Unsupported(f"{self.name} without isolation links data into the workspace, not at {target}: "
                                      "use a relative target, or isolation sandbox or container")
            return isolation, None, None
        if network:
            raise Unsupported(f"{self.name} under isolation {isolation} reaches its model only; it cannot be given "
                              f"more hosts ({network})")
        if isolation == "sandbox":
            sandbox = s.get("sandbox")
            if sandbox is None:
                raise Unsupported("isolation: sandbox needs the run's sandbox (settings.sandbox)")
            if not getattr(sandbox, "enabled", True) and (limits.hidden or network is not None):
                raise Unsupported("the run's sandbox is off: it cannot hide paths or limit the network")
            if network is not None:
                # The session's own model (its provider's host, a plan login's hosts) is allowed in any case.
                from hermit.sandbox import model_access
                _, hosts, ports = model_access([s.get("binding")])
                own = [*hosts, *codex_login.hosts(s.get("login"))]
                if getattr(sandbox, "network", None) == "open" or \
                        [h for h in getattr(sandbox, "allow", None) or () if h not in own] or \
                        [p for p in getattr(sandbox, "forward", None) or () if p not in ports] or \
                        getattr(sandbox, "sockets", None):
                    raise Unsupported("the run's sandbox lets more than the model through (an open network, allowed "
                                      "hosts, forwarded ports or sockets): it cannot limit the network to the model")
            from hermit.sandbox import SYSTEM_ETC
            # An allowlist shows what is granted: a path to keep hidden must not lie in anything granted.
            shown = [*getattr(sandbox, "readable", ()), *getattr(sandbox, "writable", ()),
                     *(b.source for b in getattr(sandbox, "binds", ())), *(m.source for m in limits.readable),
                     *(map(Path, s.get("writable") or ())), workspace,           # the session's own, bound writable
                     *([Path(os.path.expanduser(str(s["home"])))] if s.get("home") else []),
                     # and what every sandbox shows: the system, the interpreter's installation, the packages and
                     # the framework it shows (visible() lists them)
                     *map(Path, ("/usr", "/bin", "/sbin", "/lib", "/lib64", *SYSTEM_ETC)),
                     *(sandbox.visible([str(s.get("bin") or self.program)])
                       if hasattr(sandbox, "visible") else [])]           # with the CLI's own installation
            for path in limits.hidden:
                if any(_overlap(Path(path), Path(p)) for p in shown):
                    raise Unsupported(f"{path} is to be hidden but lies in what the sandbox shows")
            return isolation, None, sandbox
        shown = [workspace, *(m.source for m in limits.readable)]         # every mount the container is given
        if s.get("home"):                         # a fresh home is a new directory: nothing hidden lies in it
            shown.append(Path(os.path.expanduser(str(s["home"]))))
        for path in limits.hidden:
            if any(Path(path).resolve().is_relative_to(Path(p).resolve()) for p in shown):
                raise Unsupported(f"{path} is to be hidden but lies in what the container is given")
        conf = container.settings(s, self.backend)
        key = (self.backend, *sorted(conf.items()))
        if key not in _ready:
            problems = container.check(conf, self.backend)
            if problems:                                # else every session would fail
                raise RuntimeError("the agent's container is not ready: " + "; ".join(problems))
            _ready.add(key)
        return isolation, conf, None

    # ---------- what is the CLI's own ----------
    def session_env(self) -> dict[str, str]:
        """The variables a session of this agent is given besides `env`."""
        return {}

    def dropped_env(self) -> tuple[str, ...]:
        """This process's variables a session of this agent must not get, besides `env_drop`."""
        return ()

    def by_fd(self) -> dict[str, str]:
        """In a container, the session's variables the CLI reads from a descriptor instead ({the variable: the one
        that names its descriptor}): container.run moves them out of every environment there."""
        return {}

    def prompt_text(self, prompt: str) -> str:
        """The prompt as the CLI is to read it."""
        return prompt

    def outcome(self, raw: str, stream: Path, last_file: Path) -> dict:
        """What the session's stream says: `reply`, `served`, `unavailable` (a provider that could not serve), `refused`
        (the plan's limit), and for what stops short of that `failed`, `failure` (why) and the rest of Result.extra;
        `started` for a CLI that writes an event before it has a session (absent: it started once it wrote one)."""
        raise NotImplementedError

    # ---------- the home ----------
    def _home(self) -> tuple[Path, bool]:
        s = self.settings
        if s.get("home"):
            home = Path(os.path.expanduser(str(s["home"]))).resolve()
            home.mkdir(parents=True, exist_ok=True)
            return home, False
        # Not under the temporary directory: Codex makes no helper programs for its sandbox in a home there, and
        # without them no command of the session runs.
        homes = Path(os.path.expanduser(os.environ.get("HERMIT_HOMES", "~/.cache/hermit/homes")))
        homes.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{self.backend}-", dir=homes)).resolve(), True

    def _sign_in(self, home: Path) -> list[Path]:
        """The session's sign-in in its home; the files to remove after the call."""
        s = self.settings
        if s.get("login") == "chatgpt":
            codex_login.prepare(Path(s.get("login_root") or codex_login.ROOT), home)
            return [home / codex_login.FILE]
        if s.get("login") or s.get("binding"):         # login: claude signs in with the token in the environment
            return []
        copied = []
        for name in self.home_files if s.get("home_files") is None else s["home_files"]:
            source = self.user_home() / name
            if source.is_file():
                _clear(home / name)                     # a link the last session left would take the copy elsewhere
                shutil.copy2(source, home / name)
                os.chmod(home / name, 0o600)
                copied.append(home / name)
        return copied

    def _key_env(self) -> str | None:
        if not self.settings.get("binding"):
            return None
        from hermit.agent.model.backends import Binding
        return Binding.from_spec(self.settings["binding"]).key_env_if_set()

    # ---------- one call ----------
    def run(self, prompt, *, workspace=None, tools: Sequence[Tool] = (), limits: Limits | None = None,
            session: Any = None, roles: Sequence[dict] = ()) -> Result:
        """One session. On a login it first waits while the plan holds sessions back, runs in one of the machine's
        session slots, and is run again when the plan refused it (hermit.agent.base.login's plan)."""
        s = self.settings
        login = s.get("login")
        if not login:
            limits = self._within(limits)
            if limits is False:
                return self._not_run("deadline", None)
            return self._session(prompt, workspace=workspace, tools=tools, limits=limits, session=session,
                                 roles=roles)
        if codex_login.LOGINS.get(login) != self.backend:
            raise Unsupported(f"{self.name} does not sign in with login: {login}")
        if login == "claude" and not os.environ.get(codex_login.TOKEN_ENV):
            return self._not_run("login", None)
        plan, cancel = codex_login.plan_dir(s.get("plan_dir"), login), s.get("cancel")
        max_s = s.get("plan_max_wait_s", codex_login.MAX_WAIT_S)
        deadline = None if max_s is None else time.time() + float(max_s)    # over every wait of this session
        if s.get("deadline") is not None:
            deadline = float(s["deadline"]) if deadline is None else min(deadline, float(s["deadline"]))
        say = lambda m: print(f"hermit {self.name}: {m}", file=sys.stderr, flush=True)
        while True:
            credits = codex_login.opted_in(s.get("usage_credits"))
            waited = codex_login.wait(plan, cancel, say, None if deadline is None else max(0.0, deadline - time.time()),
                                      credits)
            if waited:
                return self._not_run(waited, max_s)
            with codex_login.slot(plan, cancel, say, deadline) as got:
                if got is not True:
                    return self._not_run(got, max_s)
                if codex_login.held(plan, credits):  # paused or held while it waited for a slot: wait again
                    continue
                left = self._within(limits)              # what the waits left of the caller's deadline
                if left is False:
                    return self._not_run("deadline", None)
                result = self._session(prompt, workspace=workspace, tools=tools, limits=left, session=session,
                                       roles=roles)
                # What the session says of the plan is written before its slot is given back: a session waiting for
                # the slot reads the plan the moment it has it, so with SLOTS: 1 the one after a session that drew
                # credits (or was refused) is held, never started in between.
                report = result.extra.pop("plan_report", None)
                if report is not None:              # a report in the session's stream, not in its home
                    codex_login.note_usage(plan, None, report)
                    if report["using_credits"] and not codex_login.opted_in(s.get("usage_credits")):
                        say("a session drew the account's usage credits, billed apart from the plan: sessions wait for "
                            "the plan's window (usage_credits: true allows them)")
                        # a hold for the sessions to come: this one does not wait on it
                        codex_login.hold(plan, report.get("credits_resets_at") or report["resets_at"], "usage_credits",
                                         cancel=cancel, max_s=0.0)
                done = result.extra.get("served") or not result.extra.get("refused") or \
                    result.extra.get("login_failed")      # a sign-in no wait restores: returned, never held or rerun
                if not done:     # never ran: no answer; every session waits for the window (the refusal's reset)
                    codex_login.hold(plan, until=result.extra.get("held_until") or None, cancel=cancel,
                                     max_s=None if deadline is None else max(0.0, deadline - time.time()))
            if done:
                return result
            if str(s.get("plan_rerun", True)).strip().lower() in ("false", "0", "no", "off"):
                say("a session was refused for the plan's usage limit; it is returned (plan_rerun: false)")
                return result
            say("a session was refused for the plan's usage limit; it runs again once the plan allows")

    def _within(self, limits: Limits | None):
        """`limits` with its timeout cut to what is left before the `deadline` setting (as given without one);
        False when nothing is left."""
        if self.settings.get("deadline") is None:
            return limits
        left = float(self.settings["deadline"]) - time.time()
        if left <= 0:
            return False
        limits = limits or Limits()
        own = limits.timeout_s or self.settings.get("timeout_s", 3600)     # as the session's own default
        return dataclasses.replace(limits, timeout_s=min(left, float(own)) if own else left)

    def _left(self, timeout: float | None):
        """`timeout` cut to what the preparation left before the `deadline` setting, just before the CLI starts;
        False when nothing is."""
        if self.settings.get("deadline") is None:
            return timeout
        left = float(self.settings["deadline"]) - time.time()
        if left <= 0:
            return False
        return min(timeout, left) if timeout else left

    def _not_run(self, why: str, max_s) -> Result:
        """A session that never started: its caller stopped it, the caller's deadline passed, the plan held it back
        past `plan_max_wait_s` (a provider that could not serve, for the caller to retry or report; no answer), or
        there is no sign-in for it."""
        tail = {"cancelled": "", "deadline": "hermit: the caller's deadline passed before the session started",
                "login": codex_login.NO_TOKEN}.get(why, f"hermit: the plan held the session back for {max_s} s")
        return Result(reply="", exit_code=-1, extra={"served": False, "unavailable": why in ("timeout", "deadline"),
                                                     "refused": False, "started": False, "failed": why == "login",
                                                     "login_failed": why == "login", "rejected": False,
                                                     "cancelled": why == "cancelled", "isolation":
                                                     self.settings.get("isolation") or "none", "wall_s": 0.0,
                                                     "failure": tail, "output_tail": tail})

    def _session(self, prompt, *, workspace=None, tools: Sequence[Tool] = (), limits: Limits | None = None,
                 session: Any = None, roles: Sequence[dict] = ()) -> Result:
        if workspace is None:
            raise Unsupported(f"{self.name} works in a workspace")
        if not isinstance(prompt, str):
            raise Unsupported(f"{self.name} takes a prompt, not chat messages")
        if not isinstance(self.settings.get("web_search"), bool):
            raise ValueError(f"{self.name}: set web_search, true or false")
        s, limits = self.settings, limits or Limits()
        if s.get("text_only") and (tools or roles):
            raise Unsupported(f"{self.name}: a session with text_only has no tools or roles")
        prompt = self.prompt_text(prompt)
        given_workspace = Path(workspace).absolute()            # the caller's spelling: a log may lie below it
        workspace = Path(workspace).resolve()
        isolation, conf, sandbox = self._isolation(workspace, limits)
        home, fresh = self._home()
        if s.get("log"):
            stream = Path(s["log"])
        elif fresh:                                     # a fresh home goes after the call; the stream stays
            stream = Path(tempfile.mkstemp(prefix=f"agent-{self.backend}-", suffix=".jsonl")[1])
        else:
            stream = home / f"agent-{self.backend}.jsonl"
        places = (home, workspace, given_workspace, *(s.get("writable") or ()),        # the session's
                  *getattr(sandbox, "writable", ()))
        _session_output(stream, places)                 # written by this process: never through a link left there
        stream.parent.mkdir(parents=True, exist_ok=True)   # after: a link left on the way is gone, not followed
        if s.get("stderr_log"):
            _session_output(s["stderr_log"], places)
        if not os.path.exists(stream):
            stream.touch()
        wrote = _identity(stream)                       # the file the session's stream goes to
        last_file = home / "last_message.txt"
        _clear(last_file)
        last_file.unlink(missing_ok=True)
        timeout = limits.timeout_s or s.get("timeout_s", 3600)       # None: no clock (the caller cancels)
        timeout = float(timeout) if timeout else None
        mounts = [Mount(Path(m.source), _target(m, workspace), m.readonly) for m in limits.readable]
        # In the run's sandbox a mount is shown at its target (a bind); paths in the prompt, the command and the
        # environment are spelled as they are inside, relative to the working directory when that is the layout's
        # root (the learner's /w).
        binds = [(m.source, sandbox.translate(str(m.target)) if hasattr(sandbox, "translate") else str(m.target),
                  not m.readonly) for m in mounts
                 if Path(m.target).resolve() != Path(m.source).resolve()] if sandbox is not None else []
        if sandbox is not None and hasattr(sandbox, "translate"):
            layout = getattr(sandbox, "layout", None)
            at_root = bool(layout) and sandbox.translate(str(workspace), binds) == layout[1]
            spell = (lambda text: sandbox.relative(text, binds)) if at_root else \
                (lambda text: sandbox.translate(text, binds))
            prompt = spell(prompt)
            roles = [{**r, "instructions": spell(str(r.get("instructions", "")))} for r in roles]
        placed: dict[str, str] = {}                     # a mount inside the workspace: bound there, or linked
        server, copied, failed = None, [], True
        try:
            copied = self._sign_in(home)
            if tools:
                from hermit.agent.base.tool_server import ToolServer
                from hermit.sandbox import inside_tools        # results name paths as the session sees them
                server = ToolServer(inside_tools(tools, sandbox, binds),
                                    host=container.gateway(conf) if conf else "127.0.0.1",
                                    mcp_only=s.get("mcp_tools")).start()
            name = s.get("tools_name") or "hermit_tools"
            own = {k: os.path.expandvars(str(v)) for k, v in (s.get("env") or {}).items()}   # $KEY_VAR -> the key
            own.update(self.session_env())
            if server is not None and s.get("tools_env"):
                own[s["tools_env"]] = server.url                  # the tools' JSON entrance, for the session's shell
            if server is not None and conf is not None and self.backend == "claude":
                # the tools' server on the network's gateway is reached directly: Claude Code sends even a plain-HTTP
                # request through HTTPS_PROXY, the egress proxy, which refuses it
                from urllib.parse import urlparse
                gateway = urlparse(server.url).hostname
                own.setdefault("NO_PROXY", gateway)
                own.setdefault("no_proxy", gateway)
            env = dict(own)
            self.prepare_home(home, roles, server.mcp_url if server else None, name)
            env.update(self.home_env(home))
            command = self.command(prompt, workspace, last_file, session=session, roles=roles,
                                   tools_url=server.mcp_url if server else None)
            started = time.time()
            if conf is not None:
                for m in mounts:
                    if m.target.is_relative_to(workspace):
                        m.target.mkdir(parents=True, exist_ok=True)   # the mount point, a real directory inside
                key_env = self._key_env()
                keys = {**env, **({key_env: os.environ.get(key_env, "")} if key_env else {})}
                timeout = self._left(timeout)
                if timeout is False:
                    return self._not_run("deadline", None)
                code, timed_out, cancelled = container.run(command, conf=conf, work=workspace, home=home,
                                                           mounts=mounts, env=keys, timeout=timeout, stdout=stream,
                                                           cancel=s.get("cancel"),
                                                           stdin=prompt if self.on_stdin() else None,
                                                           backend=self.backend, by_fd=self.by_fd())
            else:
                if isolation == "none":
                    # what is placed: a mount at another path than its source's (before a link makes them one)
                    inside = [m for m in mounts if _elsewhere(m)]
                    # the login's plan, shared with every session on the machine: hidden from this one's commands
                    plan = codex_login.plan_dir(s.get("plan_dir"), s.get("login")) if s.get("login") else None
                    if plan is not None and self.binds_mounts:
                        plan.mkdir(parents=True, exist_ok=True)
                    command = _bound(command, mounts, workspace, [plan] if plan is not None else []) \
                        if self.binds_mounts else command
                    placed = {str(m.target): "bound" if _is_bound(command, m) else "linked" for m in inside}
                    _link([m for m in mounts if not _is_bound(command, m)])
                drop = {*(s.get("env_drop") or ()), *self.dropped_env()}
                full_env = {**{k: v for k, v in os.environ.items() if k not in drop}, **env}
                if sandbox is not None:
                    # The session reaches its provider as Harbor's agents do: its key variable passes, a server on
                    # the loopback (a meter) is forwarded; the provider's host is allowed by the run's sandbox.
                    from hermit.sandbox import model_access
                    keys, _, local = model_access([s.get("binding")])
                    keep = [*own, *keys, *self.home_env(home)]          # and its home's variables
                    command, full_env = sandbox.wrap(command, writable=[home, *map(Path, s.get("writable") or ())],
                                                     env=full_env, home=home, keep=keep,
                                                     forward=[*([server.port] if server else []), *local],
                                                     readable=[m.source for m in mounts
                                                               if Path(m.target).resolve() == Path(m.source).resolve()],
                                                     binds=binds, chdir=workspace,
                                                     env_ports=limits.network is None)   # model-only: no more
                    if (limits.hidden or limits.network is not None) and \
                            not any(Path(a).name == "bwrap" for a in command):
                        raise Unsupported("the run's sandbox did not isolate the call (no bubblewrap): it cannot hide "
                                          "paths or limit the network")
                timeout = self._left(timeout)
                if timeout is False:
                    if sandbox is not None and hasattr(sandbox, "abandon"):
                        sandbox.abandon(command)         # its isolation report will not come
                    return self._not_run("deadline", None)
                try:
                    code, timed_out, cancelled = run_process(
                        command, env=full_env, cwd=workspace, stream=stream, timeout=timeout, cancel=s.get("cancel"),
                        stdin=prompt if self.on_stdin() else None,
                        stderr=Path(s["stderr_log"]) if s.get("stderr_log") else None)
                finally:                                 # ended, raised, or bwrap exited before the relay ran:
                    if sandbox is not None and hasattr(sandbox, "abandon"):
                        sandbox.abandon(command)         # a report that never came is not waited for
            wall_s = time.time() - started
            failed = False
        finally:
            if server is not None:
                server.close()
            _restore_home(home)                         # a home the session replaced: never read or written through
            if sandbox is not None and getattr(sandbox, "enabled", False):
                # What the session could write is read and written by the host after it (an Evolver's own code):
                # no link out of it and no pipe is left there.
                from hermit.sandbox import drop_unsafe
                for place in {*map(str, getattr(sandbox, "writable", ())), *map(str, s.get("writable") or ()),
                              *(str(b.source) for b in getattr(sandbox, "binds", ()) if b.writable)}:
                    drop_unsafe(place)
            if s.get("login") == "claude":
                # Outside a container a command of the session can read the login's token from a parent's environment
                # and print it (in one, nothing the session runs reaches it: CLAUDE_SANDBOX, by_fd, docker/claude/):
                # what the session leaves (its stream, its stderr, whatever its CLI kept in a kept home: transcripts,
                # tool results) never carries it, however the session ended.
                _scrub_token([*([stream] if _identity(stream) == wrote else []),
                              *([Path(s["stderr_log"])] if s.get("stderr_log") else []),
                              *([] if fresh else _files(Path(home)))])
            if s.get("login") == "chatgpt":
                codex_login.keep_refreshed(Path(s.get("login_root") or codex_login.ROOT), home)
                codex_login.note_usage(codex_login.plan_dir(s.get("plan_dir")), home)
            for path in copied:
                path.unlink(missing_ok=True)                              # the sign-in does not outlive the call
            if failed and fresh:
                shutil.rmtree(home, ignore_errors=True)
        # The stream is read back only from the file this process wrote: a session that could write its directory
        # and put a link (or a pipe, or another file) there has left nothing of it to read.
        readable = stream if wrote is not None and _identity(stream) == wrote else Path(os.devnull)
        raw = readable.read_text(encoding="utf-8", errors="replace") if readable is stream else ""
        said = self.outcome(raw, readable, last_file)
        said["failed"] = said.get("failed", False) and not timed_out and not cancelled
        began = said.pop("started", None)              # the CLI's own word, else: it wrote an event
        handle, usage = self.parse(readable, (s.get("binding") or {}).get("ref") or s.get("model") or self.backend)
        usage = self.adjust_usage(home, handle, usage, resumed=bool(session))
        price_usage(usage, s)
        steps = self.steps(events(readable))
        if fresh:
            shutil.rmtree(home, ignore_errors=True)
        return Result(reply=said.pop("reply"), finish_reason=said.pop("finish_reason", None), trajectory=steps,
                      usage=usage, session=handle, exit_code=code, timed_out=timed_out, log=stream,
                      extra={"failed": False, "login_failed": False, "rejected": False, **said,
                             "started": any(line.lstrip().startswith("{") for line in raw.splitlines())
                             if began is None else bool(began),
                             "cancelled": cancelled, "isolation": isolation, "wall_s": round(wall_s, 1),
                             "mounts": placed, "output_tail": raw[-2000:]})

    def on_stdin(self) -> bool:
        """Whether the prompt is given on stdin, not on the command line."""
        return bool(self.settings.get("stdin"))

    def instructed(self, system, prompt):
        """This agent for a call that takes `system` as its instructions, where the CLI takes them (system_settings)."""
        settings, prompt = self.system_settings(system, prompt)
        return self.configured(**settings), prompt


class Codex(CliAgent):
    name = backend = program = "codex"
    label = "Codex"
    isolations = ISOLATIONS
    home_var, user_folder = "CODEX_HOME", ".codex"
    home_files = (codex_login.FILE,)
    turn_done = "turn.completed"

    def prepare_home(self, home, roles, tools_url, tools_name):
        write_home(home, self.settings, roles, tools_url, tools_name)

    @classmethod
    def user_home(cls) -> Path:
        return codex_login.source().parent

    @classmethod
    def web_search_args(cls, enabled: bool) -> list[str]:
        """Codex's hosted search (`web_search`, `live` or `disabled`): it runs at the provider, so no sandbox,
        container or proxy can stop it, only this setting does."""
        return ["-c", f'web_search="{'live' if enabled else 'disabled'}"']

    def system_settings(self, system, prompt):
        extra, prompt = codex_instructions(system, prompt)
        return {"args": [*(self.settings.get("args") or []), *extra]}, prompt

    @classmethod
    def errors(cls, event):
        """`error`, `turn.failed` and an `error` item."""
        kind, item = event.get("type"), event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "error":
            return [str(event.get("message") or "")]
        if kind == "turn.failed":
            error = event.get("error")
            return [str(error.get("message") if isinstance(error, dict) else error or "")]
        if kind == "item.completed" and item.get("type") == "error":
            return [str(item.get("message") or "")]
        return []

    @classmethod
    def last_message(cls, log, last=None):
        """Codex's `-o` file (never read through a link the session left), else the last message in its stream."""
        if last is not None and not last.is_symlink() and last.is_file() and last.read_text(errors="replace").strip():
            return last.read_text(errors="replace")
        replies = [s.text for s in codex_steps(events(log)) if s.kind == "message"]
        return replies[-1] if replies else ""

    @classmethod
    def parse(cls, log, model):
        return parse_codex(log, model)

    @classmethod
    def steps(cls, stream):
        return codex_steps(stream)

    def adjust_usage(self, home, handle, usage, resumed):
        """Codex reports a thread's running total: a resumed thread's earlier calls are taken off (_this_call)."""
        return [_this_call(home, str(handle), usage[0], resumed=resumed)] if handle and usage else usage

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        s = self.settings
        effort = s.get("effort")
        hooks = (workspace / ".codex" / "hooks.json").is_file()     # the deployment's hooks, trusted as deployed
        return [str(s.get("bin") or self.program), "exec", "--json", "--skip-git-repo-check",
                "-C", str(s.get("cwd") or workspace),
                "-s", s.get("codex_sandbox") or "workspace-write", "-o", str(last_file),
                *(["-m", s["model"]] if s.get("model") else []),
                *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
                *(["--dangerously-bypass-hook-trust"] if hooks else []),
                *(["--enable", "multi_agent"] if roles else []), *(s.get("args") or []),
                *(["resume", str(session)] if session else []), *(["-"] if s.get("stdin") else [prompt])]

    def outcome(self, raw, stream, last_file):
        notes, done = self.notices(raw), self.served(raw)
        return {"reply": self.last_message(stream, last_file), "served": done,
                "unavailable": bool(UNAVAILABLE.search(notes)), "refused": codex_login.refused(notes) and not done,
                "failure": notes[-600:]}


class Claude(CliAgent):
    name = backend = program = "claude"
    label = "Claude Code"
    aliases = ("claude-code",)
    home_var, user_folder = "CLAUDE_CONFIG_DIR", ".claude"
    home_files = (".credentials.json",)
    turn_done = "result"
    isolations = ISOLATIONS                       # in a container of its own too (docker/claude/), as Codex

    @classmethod
    def web_search_args(cls, enabled: bool) -> list[str]:
        """Claude Code's WebSearch and WebFetch tools, denied when off."""
        return [] if enabled else ["--disallowedTools", "WebSearch,WebFetch"]

    def system_settings(self, system, prompt):
        """A session that only replies takes `system` in place of the CLI's own instructions, which describe tools it
        does not have; one that works keeps them, and `system` follows them."""
        if self.settings.get("text_only"):
            return {"system_prompt": system}, prompt
        return {"append_system_prompt": system or None}, prompt

    @classmethod
    def errors(cls, event):
        """A failed `result` with its own diagnostics, and an `error` event."""
        kind = event.get("type")
        if kind == "result" and (event.get("is_error") or str(event.get("subtype") or "").startswith("error")):
            errors = event.get("errors")
            return [str(event.get("result") or event.get("subtype") or ""),
                    *([str(x) for x in errors if x] if isinstance(errors, list) else [str(errors)] if errors else [])]
        if kind == "error":
            return [json.dumps(event.get("error") or event, default=str)]
        return []

    @classmethod
    def last_message(cls, log, last=None):
        """The text of its last `result`."""
        text = ""
        for event in events(log):
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                text = event["result"]
        return text

    @classmethod
    def parse(cls, log, model):
        return parse_claude(log, model)

    @classmethod
    def steps(cls, stream):
        return claude_steps(stream)

    @classmethod
    def served(cls, raw):
        """Its last `result` says so: a failure ends with a result too (Claude Code's exit code 1, often of
        subtype `success` with `is_error` set), and a prompt it took for one of its own commands with a result of no
        turn."""
        return _succeeded(_last_result(_objects(raw)[0]))

    binds_mounts = True
    efforts = ("low", "medium", "high", "xhigh", "max")      # what --effort takes; it warns and ignores anything else

    def on_stdin(self):
        """Always: on the command line a prompt that starts with `-` is taken for an option (`--version` prints the
        version, as the session's whole output), and the CLI reads a non-terminal stdin to its end all the same."""
        return True

    def prompt_text(self, prompt):
        """The task's or caller's text, for the model: the CLI runs a prompt that starts with `/` as one of its commands
        (`/cost` never reaches the model, and comes back as a reply), and a leading line break stops that."""
        return f"\n{prompt}" if prompt.startswith("/") else prompt

    def session_env(self):
        s, env = self.settings, {}
        if s.get("text_only"):
            env.update(CLAUDE_TEXT_ENV)
        if s.get("login") == "claude":
            named = sorted(set(s.get("env") or ()) & set(CLAUDE_BILLING))
            if named:
                raise Unsupported(f"login: claude is the subscription; `env` names what would pay instead: {named}")
            env.update({**CLAUDE_LOGIN_ENV, codex_login.TOKEN_ENV: os.environ[codex_login.TOKEN_ENV]})
        return env

    def dropped_env(self):
        return (*CLAUDE_BILLING, *CLAUDE_INHERITED) if self.settings.get("login") == "claude" else ()

    def by_fd(self):
        """The login's token, in a container: Claude Code reads it from a descriptor, so neither its environment nor its
        commands' holds it (and the image's CLI is undumpable: nothing beside it reads it from its memory)."""
        return {codex_login.TOKEN_ENV: CLAUDE_TOKEN_FD} if self.settings.get("login") == "claude" else {}

    def _prompt_file(self, flag: str, text: str, home: Path) -> list[str]:
        """A system text for the command line, or past what one argument may hold in a file of the session's home, the
        one place every isolation shows it."""
        if len(text.encode()) <= ARG_MAX:
            return [flag, text]
        path = home / f"{flag.lstrip('-')}.md"         # system-prompt.md, append-system-prompt.md: one file each
        _clear(path)                                 # a link the last session left would take the text elsewhere
        path.write_text(text, encoding="utf-8")
        return [f"{flag}-file", str(path)]

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        s = self.settings
        text = bool(s.get("text_only"))
        args = [str(a) for a in (s.get("args") or [])]
        locked = s.get("login") == "claude"
        sandboxed = s.get("isolation") == "container" and not text      # its commands in Claude Code's own sandbox
        last = {**(json.loads(CLAUDE_LOCK) if locked else {}),
                **({"sandbox": CLAUDE_SANDBOX, "permissions": CLAUDE_OWN_SETTINGS} if sandboxed else {})}
        if last and any(a == "--settings" or a.startswith("--settings=") for a in args):
            raise Unsupported("login: claude and a working session in a container keep their own --settings last, "
                              "which a later one would replace: give the settings another way")
        if locked and not s.get("model"):          # the CLI's default model differs by sign-in: say which one
            raise Unsupported("login: claude runs the model it is given by name (a Claude Code model name): give one")
        if s.get("effort") and s["effort"] not in self.efforts:   # the CLI would warn and go on at its default
            raise Unsupported(f"Claude Code takes effort {', '.join(self.efforts)}, not {s['effort']!r}")
        mcp = {"mcpServers": {s.get("tools_name") or "hermit_tools": {"type": "http", "url": tools_url}}}
        agents = {r["name"]: {"description": r["description"], "prompt": r["instructions"]} for r in roles}
        stream = (s.get("output_format") or "stream-json") == "stream-json"
        if (locked or text) and (not stream or any(a.startswith("--output-format") for a in args)):
            # the payer, the plan's report and a reply that only replies are read from the stream's events
            raise Unsupported("login: claude and a session that only replies take output_format stream-json alone")
        tools = [] if text else s.get("allowed_tools")
        system = s.get("system_prompt")
        return [str(s.get("bin") or self.program), "-p",
                "--output-format", s.get("output_format") or "stream-json", *(["--verbose"] if stream else []),
                # a working session's hooks in its log: what a hook told the model is not in the stream otherwise
                *(["--include-hook-events"] if stream and not text else []),
                # --restricted refuses bypassPermissions; with no tools no permission is ever asked for
                "--permission-mode", "default" if text else s.get("permission_mode") or "bypassPermissions",
                *(["--model", s["model"]] if s.get("model") else []),
                *(["--effort", s["effort"]] if s.get("effort") else []),
                *(["--restricted", "--strict-mcp-config", "--disable-slash-commands"] if text else []),
                *(["--tools", ",".join(tools)] if tools is not None else []),
                *(["--no-session-persistence"] if text or s.get("persist_session") is False else []),
                *(self._prompt_file("--system-prompt", system, last_file.parent) if system is not None else []),
                *(self._prompt_file("--append-system-prompt", s["append_system_prompt"], last_file.parent)
                  if s.get("append_system_prompt") else []),
                # one list, web search's (web_search_args) and the tools that outlive the session
                "--disallowedTools", ",".join([*([] if s.get("web_search") else ["WebSearch", "WebFetch"]),
                                               *CLAUDE_BEYOND_SESSION]),
                *(["--mcp-config", json.dumps(mcp)] if tools_url else []),
                *(["--agents", json.dumps(agents, ensure_ascii=False)] if agents else []),
                *(["--resume", str(session)] if session else []), *args,
                *(["--settings", json.dumps(last)] if last else [])]

    def outcome(self, raw, stream, last_file):
        """Read from the CLI's own fields: a failed result's `api_error` and status and the failed `assistant` event's
        `error`; its wordings only for a stream without them, and never a message, a command's output or a file."""
        objects, _ = _objects(raw)
        s, result = self.settings, _last_result(objects)
        init = next((e for e in objects if e.get("type") == "system" and e.get("subtype") == "init"), None) or {}
        broke = ([e for e in objects if e.get("type") == "assistant" and (e.get("error") or e.get("is_api_error_message"))]
                 or [{}])[-1]
        done, kind = _succeeded(result), str(broke.get("error") or "")
        result_ = result or {}
        api_error = result_.get("api_error") or broke.get("api_error")
        status = result_.get("api_error_status") or broke.get("api_error_status")
        notes = self.notices(raw)
        # A result's own words; only a stream without one has nothing better than what the CLI printed beside its events.
        errors = result_.get("errors") if isinstance(result_.get("errors"), list) else []
        said = " ".join(map(str, [result_.get("result") or "", *errors])) if result else notes
        problem, login_lost = "", False
        if s.get("login") and str(init.get("apiKeySource") or "none") != "none":
            # The lock and the dropped variables should make this impossible; the bill is what the login is for.
            problem = f"the session paid with {init['apiKeySource']}, not the subscription"
            done, login_lost = False, True
        elif s.get("text_only") and (init.get("tools") or init.get("mcp_servers")):
            problem = "the session was to have no tools, and it reported some"
            done = False
        limit = not done and (api_error == "usage_limit_reached" or (bool(CLAUDE_LIMIT.search(said))
                                                                     and "not your usage limit" not in said.lower()))
        auth = not done and not limit and (kind in CLAUDE_LOGIN_LOST or bool(CLAUDE_AUTH.search(said)))
        busy = not done and not limit and not auth and not problem and (
            kind in CLAUDE_BUSY or api_error == "connection_failed"
            or (isinstance(status, int) and (status == 429 or status >= 500))
            or bool(CLAUDE_DOWN.search(said)) or bool(UNAVAILABLE.search(said)))
        # What a retry cannot change: the request or the model's answer to it. No login means a backend's key: its fault
        # is the run's configuration, not a sign-in to restore.
        login_failed = (auth or login_lost) and bool(s.get("login"))
        rejected = not done and not limit and not busy and not login_failed and (
            auth or bool(problem) or kind in CLAUDE_REJECTED or result_.get("stop_reason") == "refusal"
            or result_.get("terminal_reason") == "prompt_too_long" or status in (400, 404))
        info = (broke.get("api_error_params") or {}).get("rate_limit_info") if isinstance(
            broke.get("api_error_params"), dict) else None
        report = codex_login.claude_report(objects) if s.get("login") == "claude" else None
        reply = ""
        if done:
            # With no tools every assistant message is the reply: the CLI continues a message cut at its output limit
            # by itself, and keeps only the last part (the last text block) as `result`.
            reply = "".join(step.text for step in claude_steps(objects) if step.kind == "message") \
                if s.get("text_only") else _final_turn(objects) or (
                    result_.get("result") if isinstance(result_.get("result"), str) else "")
        used = result_.get("model_usage_since_last_result") or result_.get("modelUsage")
        out = {"reply": reply, "served": done, "unavailable": busy, "refused": limit, "failed": not done,
               "login_failed": login_failed, "rejected": rejected,
               "model_refusal": result_.get("stop_reason") == "refusal",     # the model declined (a retry cannot help)
               "failure": (problem or notes)[-600:],
               "finish_reason": FINISH.get(result_.get("stop_reason"), "stop") if done else None,
               "held_until": (info or {}).get("resetsAt") or (report or {}).get("resets_at"),
               "served_model": next(iter(used)) if isinstance(used, dict) and len(used) == 1 else None}
        return {**out, **({"plan_report": report} if report is not None else {})}


CLAUDE_CONTINUE = "Output token limit hit."   # the CLI's own words when it continues a message cut at its output limit


def _final_turn(objects: list[dict]) -> str:
    """The text of a working session's last turn, the final message as Codex's last message is one: every text block
    the session's own assistant events gave after its last tool call and result, after the last text the harness put
    in the conversation (a Stop hook that sent the model back to work), and after the last turn a result closed (a
    session whose sub-agents work in the background is handed their result in a turn of its own). A message the CLI
    continued past its output limit is one text again. `result` holds only the last block (a message of several, or one
    the CLI continued, loses the rest)."""
    texts: list[str] = []
    glue = closed = False
    for event in objects:
        if event.get("type") == "result" and not event.get("parent_tool_use_id"):
            closed = True                         # a turn ended: what comes after is another turn
            continue
        if event.get("parent_tool_use_id") or event.get("type") not in ("assistant", "user") or event.get("error"):
            continue                              # a sub-agent's, the CLI's own, a failed request's
        if closed:
            texts, glue, closed = [], False, False
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        blocks = [b for b in message.get("content") or [] if isinstance(b, dict)] \
            if isinstance(message.get("content"), list) else []
        if any(b.get("type") in ("tool_use", "server_tool_use", "tool_result") for b in blocks):
            texts, glue = [], False               # a call or its result: what came before was not the final message
        elif event["type"] == "user":
            said = [str(b.get("text") or "") for b in blocks if b.get("type") == "text"] or (
                [message["content"]] if isinstance(message.get("content"), str) else [])
            if any(s.startswith(CLAUDE_CONTINUE) for s in said):
                glue = True                       # the CLI continues the message it cut
            elif any(s.strip() for s in said):
                texts, glue = [], False           # the harness spoke (a Stop hook's feedback): the model answers again
        else:
            for text in (b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)):
                if glue and texts:
                    texts[-1] += text             # the rest of the message the CLI cut, where it was cut
                else:
                    texts.append(text)
                glue = False
    return "\n\n".join(t for t in texts if t.strip())


PI_PROVIDER = "hermit"      # the models.json provider a bound model is given under
PI_SETTINGS = {"cacheWarming": "off",                    # no provider requests besides the session's own
               "enableInstallTelemetry": False}


class Pi(CliAgent):
    """Pi (`pi --mode json`). Its home is its agent directory (PI_CODING_AGENT_DIR) and its HOME, offline (no catalog,
    update or download requests); a bound model is the models.json provider `hermit`, its key read by Pi from the
    backend's variable; the call's tools are an MCP server whose tools the model is offered directly. Pi reports a
    failed request in its stream and exits 0: whether the session failed is read from the stream (`outcome`)."""
    name = backend = program = "pi"
    label = "Pi"
    home_var, user_folder = "PI_CODING_AGENT_DIR", ".pi/agent"
    home_files = ("auth.json",)
    turn_done = "agent_settled"

    def home_env(self, home):
        """Every variable Pi reads goes here, so that it passes the sandbox's filter: its agent directory, its HOME
        (Pi reads ~/.agents/skills), offline, no telemetry; a session directory of the caller's emptied, which Pi
        reads as unset (its sessions stay in the home, where a resume finds them)."""
        return {"PI_CODING_AGENT_DIR": str(home), "HOME": str(home), "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
                "PI_CODING_AGENT_SESSION_DIR": ""}

    @classmethod
    def user_home(cls) -> Path:
        return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / cls.user_folder)

    def prepare_home(self, home, roles, tools_url, tools_name):
        """models.json (the bound model), mcp.json (the call's tools, offered directly rather than through Pi's code
        mode) and settings.json. No host path is written in them: a sandbox translates only the command, the
        environment and the prompt."""
        if roles:
            raise Unsupported("pi has no sub-agents: it takes no roles")
        s = self.settings
        _clear(home / "models.json")
        (home / "models.json").unlink(missing_ok=True)
        if s.get("provider"):
            provider = json.loads(json.dumps(s["provider"]))
            if s.get("effort") and s["effort"] != "off":          # --thinking applies to a reasoning model only
                for model in provider.get("models") or []:
                    model["reasoning"] = True
            if s.get("image_input"):          # else Pi sends the model no image (a text-only model refuses them)
                for model in provider.get("models") or []:
                    model["input"] = ["text", "image"]
            (home / "models.json").write_text(json.dumps({"providers": {PI_PROVIDER: provider}}, indent=1))
        # A tool call may take minutes (a Trainer tool runs rollouts): Codex's tool_timeout_sec, not Pi's 60 s
        servers = {tools_name: {"url": tools_url, "exposure": "direct", "timeout": 1800}} if tools_url else {}
        for name, data in (("mcp.json", {"mcpServers": servers}), ("settings.json", PI_SETTINGS)):
            _clear(home / name)
            (home / name).write_text(json.dumps(data, indent=1))

    @classmethod
    def web_search_args(cls, enabled: bool) -> list[str]:
        """Pi has no web search: off is its only setting."""
        if enabled:
            raise Unsupported("pi has no web search: set web_search to false")
        return []

    def system_settings(self, system, prompt):
        return {"append_system_prompt": system}, prompt

    @staticmethod
    def _last_assistant(raw: str) -> dict:
        found = {}
        for event in _objects(raw)[0]:
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if event.get("type") == "message_end" and message.get("role") == "assistant":
                found = message
        return found

    @classmethod
    def served(cls, raw):
        """The session settled, not aborted, and its last request did not fail."""
        settled = [e for e in _objects(raw)[0] if e.get("type") == cls.turn_done]
        return bool(settled) and not settled[-1].get("aborted") and \
            cls._last_assistant(raw).get("stopReason") not in ("error", "aborted")

    @classmethod
    def errors(cls, event):
        """A request that ended in an error or an abort, a retry that gave up, a failed compaction."""
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if kind == "message_end" and message.get("role") == "assistant" and \
                message.get("stopReason") in ("error", "aborted"):
            return [str(message.get("errorMessage") or f"request {message['stopReason']}")]
        if kind == "auto_retry_end" and not event.get("success"):
            return [str(event.get("finalError") or "retry failed")]
        if kind in ("compaction_end", "extension_error") and (event.get("errorMessage") or event.get("error")):
            return [str(event.get("errorMessage") or event.get("error"))]
        return []

    def outcome(self, raw, stream, last_file):
        """Pi's JSON mode exits 0 however the session ended: one that did not settle, aborted, or whose last request
        failed, was aborted or was cut at the output limit (Pi's text mode exits 1 for each) has failed."""
        notes, done = self.notices(raw), self.served(raw)
        cut = self._last_assistant(raw).get("stopReason") == "length"
        return {"reply": self.last_message(stream, last_file), "served": done,
                "unavailable": bool(UNAVAILABLE.search(notes)), "refused": False, "failed": not done or cut,
                "failure": (notes or ("the reply was cut at its output limit" if cut else "the session did not settle")
                            )[-600:]}

    def adjust_usage(self, home, handle, usage, resumed):
        """A bound model's cost is left to its price (price_usage): Pi's own figure counts a model whose
        models.json entry has no cost as free, and follows no price the caller gave."""
        if self.settings.get("binding"):
            for u in usage:
                u.cost_usd = None
        return usage

    @classmethod
    def last_message(cls, log, last=None):
        """The text of the last assistant message."""
        text = ""
        for event in events(log):
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if event.get("type") == "message_end" and message.get("role") == "assistant":
                content = message.get("content")
                text = content if isinstance(content, str) else "".join(
                    str(b.get("text") or "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")
        return text

    @classmethod
    def parse(cls, log, model):
        return parse_pi(log, model)

    @classmethod
    def steps(cls, stream):
        return pi_steps(stream)

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        s, home = self.settings, last_file.parent                      # the session's home
        system = s.get("append_system_prompt")
        if system and len(str(system).encode()) > ARG_MAX:               # too long for one argument
            _clear(home / "append_system.md")
            (home / "append_system.md").write_text(str(system))
            system = str(home / "append_system.md")                    # Pi reads a file named here
        resume = _pi_session(home, session) if session else None
        return [str(s.get("bin") or self.program), "--mode", "json",
                *(["--provider", PI_PROVIDER] if s.get("provider") else []),
                *(["--model", s["model"]] if s.get("model") else []),
                *(["--thinking", s["effort"]] if s.get("effort") else []),
                *(["--append-system-prompt", system] if system else []),
                *self.web_search_args(bool(s.get("web_search"))),
                *(["--session", str(resume)] if resume else []), *(s.get("args") or []),
                *([] if s.get("stdin") else ["--", prompt])]


def _pi_session(home: Path, handle) -> Path | None:
    """The file of Pi session `handle` in `home` (sessions/<its directory>/<time>_<id>.jsonl): resumed by its path,
    which Pi opens wherever the session ran (by its id alone, Pi asks on the terminal before it forks a session of
    another directory). None when the home keeps no such regular file: the call starts a new session."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", str(handle)):
        return None
    root = Path(home) / "sessions"
    found = [p for p in root.glob(f"*/*_{handle}.jsonl") if _regular(p) and not p.parent.is_symlink()]
    return sorted(found)[-1] if found else None


DSH_PROVIDER = "hermit"                 # the pi-ai route (`llm-pi-ai`) a bound model is given under
DSH_ROUTE = "deepseek-official"         # dsh's own route, for a model of its own provider (no binding)
DSH_PATCH = "hermit.patch.yml"          # the session's patch layer, in its home (`--patch`)
# Rows of dsh's tree off in every session: no telemetry export, no title request, no plugin list sent along with a
# request to DeepSeek's own API (nor the session log, a row of its own below; such a request still carries dsh's
# anonymous user id and the session's id), so the session's requests are the only ones; and no sub-agents or
# workflows, whose usage dsh's stream does not report (sub-agents are Codex's only when a role asks for them)
DSH_OFF = ("session-telemetry-otel", "session-title-llm", "plugin-package-inventory-deepseek", "tool-subagent",
           "tool-subagent-fork", "tool-subagent-control", "tool-workflow")
DSH_PERMISSION_MODES = ("read-only", "workspace-write", "danger-full-access")
DSH_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")   # the reasoning levels of dsh's pi-ai routes
DSH_OWN_EFFORTS = ("off", "low", "high", "max")      # those of its own route (DSH_ROUTE, DeepSeek's API): no binding
# The variable a route names for a server that needs no key: a route that names none gets no key from pi-ai under
# the name `hermit`, and dsh refuses the request (PI_AI_ERROR "No API key")
DSH_NO_KEY = "HERMIT_DSH_NO_KEY"
# dsh's cap on the project's instructions as the model is given them (agent-instructions' maxBytes), and what dsh puts
# around one AGENTS.md at the project's root within it (its <system-reminder> block, the block's opening sentences and
# the file's heading, 0.2.2): an AGENTS.md longer than the difference is cut, which dsh tells the model only
DSH_INSTRUCTION_BYTES = 65536
DSH_INSTRUCTION_FRAME = 304
# dsh's report of an MCP row that did not start (`<id> (<package>): <error>`): the session then runs without its tools
DSH_TOOLS_DOWN = re.compile(r"^(mcp-[A-Za-z0-9_-]{1,32}) \(@deepseek-ai/dsh-mcp-client\): (.*)$", re.M)
# The session's transcripts as plain JSON lines (dsh compresses them with zstd), so that hermit.redact scrubs a key a
# session wrote from its home as from every record; the row's root is dsh's own, an expression (`!!js`) of the home
# as dsh sees it, which a sandbox shows at another path than this process's
DSH_TRANSCRIPTS = ('{"id": "session-persistence-jsonl", "config": {"root": !!js "dshHomePath(\'sessions\')", '
                   '"compression": "none"}}')
# Where dsh's web search goes, whatever the session's model: DeepSeek's own API, on this key
DSH_SEARCH_HOST, DSH_SEARCH_KEY = "api.deepseek.com", "DEEPSEEK_API_KEY"


def dsh_problems(settings: dict, who: str) -> list[str]:
    """What every session of a dsh agent would refuse of its settings, found before the first one rather than at each
    session, after its task's or its learner step's rollouts: an effort its model's route has not (dsh's own, for a
    model of no binding, has fewer levels than a bound model's pi-ai route) or one with no model to take it; a sandbox
    of dsh's own it has not. The Evolver runs dsh's sandbox off and gives its sessions no instruction file
    (AgentEvolver.settings): a setting of either is refused rather than dropped. `settings` are the agent's params as
    bound (the run's default model in; the Evolver's method's own params under the run's); `who` is its section of
    the run config, agent or evolver."""
    effort, mode = settings.get("effort"), settings.get("permission_mode")
    effort = "off" if effort is False else effort                       # YAML reads a bare `off` as false
    levels, route = (DSH_EFFORTS, "a bound model") if settings.get("provider") else \
        (DSH_OWN_EFFORTS, "a model of dsh's own provider (DeepSeek's API)")
    out = []
    if effort is not None and str(effort) not in levels:
        out.append(f"{who}.model.effort is one of {', '.join(levels)} for {route}, not {effort!r}")
    if effort is not None and not settings.get("model"):
        out.append(f"{who}.model.effort needs {who}.model for dsh: it is that model's reasoningEffort")
    if who == "agent" and mode is not None and mode not in DSH_PERMISSION_MODES:
        out.append(f"agent.dsh.permission_mode is one of {', '.join(DSH_PERMISSION_MODES)} (dsh's own sandbox), "
                   f"not {mode!r}")
    if who == "evolver" and mode is not None:
        out.append("evolver.dsh.permission_mode is not taken: the dsh Evolver runs dsh's own sandbox off "
                   "(danger-full-access), the evolver sandbox being the boundary, as for Codex")
    if who == "evolver" and settings.get("instruction_files") is not None:
        out.append("evolver.dsh.instruction_files is not taken: the dsh Evolver's sessions read no AGENTS.md (an "
                   "adapter's is the acting agent's policy)")
    return out


class Dsh(CliAgent):
    """DeepSeek Harness (`dsh --profile headless --json`). Its home is DSH_HOME and its HOME (dsh reads ~/.agents),
    where dsh makes the headless profile on first use. The session's settings are a patch layer of dsh's own (rows
    of its plugin tree, `--patch`), written in the home: a bound model as the pi-ai route `hermit`, its key read by
    dsh from the backend's variable, made the default model; the call's tools as an MCP server, whose tools the model
    is offered as its own (`mcp__<server>__<tool>`); web search and fetch off unless asked for (search is DeepSeek's
    own service: with a model of another backend, fetch only); no telemetry export and no title request, no
    sub-agents (DSH_OFF); the workspace as the project, as in the run's sandbox: its own skills (.dsh/skills) and
    instructions only, never those of a folder above it (a repository it lies in). dsh runs commands in its own
    sandbox (`permission_mode`, default workspace-write) and is never asked for an approval (`--approval-policy
    never`: what would need one is refused). It exits 0 for a run SIGTERM stopped too: whether the run failed is read
    from its end (`outcome`)."""
    name = backend = program = "dsh"
    label = "DeepSeek Harness"
    home_var, user_folder = "DSH_HOME", ".dsh"
    home_files = (".env",)                        # dsh reads the keys of its own provider from $DSH_HOME/.env
    turn_done = "final"
    binds_mounts = True                           # dsh's glob and grep run ripgrep without --follow

    def home_env(self, home):
        """The variables that set where dsh reads its own files from, here, so that they pass the sandbox's filter:
        its home, HOME, and the shared agents' folder dsh reads skills and AGENTS.md from (~/.agents unless
        DSH_AGENTS_HOME says another), so none of the user's own is read; telemetry off; the placeholder key of a
        server that needs none (DSH_NO_KEY). dsh also reads a `.env` in the working directory (one that sets a name
        dsh keeps for its launch, such as PATH or PYTHONPATH, stops it before it writes an event) and in its home."""
        from hermit.agent.model.backends import PLACEHOLDER_KEY
        return {"DSH_HOME": str(home), "HOME": str(home), "DSH_AGENTS_HOME": str(home / ".agents"),
                "DSH_TELEMETRY_DISABLED": "1", DSH_NO_KEY: PLACEHOLDER_KEY}

    @classmethod
    def user_home(cls) -> Path:
        return Path(os.environ.get("DSH_HOME") or Path.home() / cls.user_folder)

    def dropped_env(self):
        """The caller's own dsh variables (`DSH_*`: another permission mode, a folder of skills, a telemetry endpoint,
        a web provider, ...; and DEEPSEEK_SEARCH_BASE_URL, where its web search goes), which would change the session
        without the sandbox: its own are home_env's."""
        return tuple(name for name in os.environ if name.startswith("DSH_") or name == "DEEPSEEK_SEARCH_BASE_URL")

    def prepare_home(self, home, roles, tools_url, tools_name):
        """The patch layer (DSH_PATCH): dsh's own rows, by their ids. A row's `config` replaces the row's, so a row is
        given whole (the instructions' maxBytes is dsh's own). No host path is written in it: a sandbox translates
        only the command, the environment and the prompt; the skills' folder is the working directory's, the
        transcripts' root dsh's own expression (DSH_TRANSCRIPTS)."""
        if roles:
            raise Unsupported("dsh: roles are not passed to DeepSeek Harness; give it none")
        if tools_url and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", tools_name):
            raise ValueError(f"dsh takes an MCP server name of 32 characters at most ([A-Za-z0-9_-]), not "
                             f"{tools_name!r}")
        s = self.settings
        rows: list[dict] = [*({"id": row, "disabled": True} for row in DSH_OFF),
                            {"id": "session-log-deepseek", "config": {"enabled": False}},
                            # The workspace is the project, wherever it lies: the skills of its own .dsh/skills only
                            # (not those of the nearest folder above with a .git, a repository the run sits in, nor
                            # the user's), and the instructions of the working directory, the workspace (no root
                            # marker: dsh reads none above it, a repository's or a deployment's around it); none with
                            # instruction_files false (dsh also adds a folder's AGENTS.md below the working directory
                            # when its file tools touch the folder)
                            {"id": "skill-filesystem", "config": {"includeDefaultRoots": False,
                                                                  "customSkillDirs": [".dsh/skills"]}},
                            {"id": "agent-instructions", "config": {
                                "maxBytes": DSH_INSTRUCTION_BYTES if s.get("instruction_files", True) else 0,
                                "projectRootMarkers": []}}]
        binding = s.get("binding") or {}
        if not s.get("web_search"):
            rows.append({"id": "tool-web", "disabled": True})          # web search and fetch, the model's tools
        elif s.get("provider") and (urllib.parse.urlparse(str(binding.get("base_url") or "")).hostname,
                                    binding.get("api_key_env")) != (DSH_SEARCH_HOST, DSH_SEARCH_KEY):
            # dsh's search is DeepSeek's own service (its Messages API on DEEPSEEK_API_KEY), whatever the session's
            # model: with a model reached elsewhere or on another key it would search on another provider and key
            # than the run's, outside its usage. Fetch only, as Codex's hosted search is off for a backend other than
            # OpenAI's
            rows.append({"id": "tool-web", "config": {"search": False, "fetch": True}})
        effort = "off" if s.get("effort") is False else str(s["effort"]) if s.get("effort") else None
        levels = DSH_EFFORTS if s.get("provider") else DSH_OWN_EFFORTS          # a bound model's route, or dsh's own
        if effort and effort not in levels:
            raise ValueError(f"dsh: an effort is one of {', '.join(levels)}"
                             f"{'' if s.get('provider') else ' for a model of its own provider'}, not {effort!r}")
        if effort and not s.get("model"):
            raise ValueError("dsh: an effort needs its model (the `model` setting): it is that model's reasoningEffort")
        default, chosen = {"provider": DSH_ROUTE}, {}
        if s.get("provider"):
            provider = json.loads(json.dumps(s["provider"]))
            provider.setdefault("apiKeyEnv", DSH_NO_KEY)              # a server that needs no key: the placeholder
            for model in provider.get("models") or []:
                if effort:       # a declared model reasons only at the levels it lists; off: it does not reason
                    model.setdefault("reasoningEfforts", False if effort == "off" else {effort: effort})
                if s.get("image_input"):          # else dsh sends the model no image (a text-only model refuses them)
                    model["input"] = ["text", "image"]
                if model.get("id") == s.get("model"):
                    chosen = model
            rows.append({"id": "llm-pi-ai", "config": {"providers": {DSH_PROVIDER: provider}}})
            default["provider"] = DSH_PROVIDER
        if s.get("model"):
            default["model"] = str(s["model"])
            if effort and chosen.get("reasoningEfforts") is not False:    # a model that does not reason takes none
                default["reasoningEffort"] = effort
            rows.append({"id": "agent-default-model", "config": default})
        elif s.get("provider"):
            raise ValueError("dsh: a provider route needs its model (the `model` setting)")
        if tools_url:
            # failOnStartupError: a server dsh cannot reach fails its row at once (dsh says so on stderr and goes on,
            # and `outcome` fails the session) rather than being retried while the session runs without the tools
            rows.append({"insert": [{"id": f"mcp-{tools_name}", "name": "@deepseek-ai/dsh-mcp-client",
                                     "config": {"serverName": tools_name, "transport": "streamable-http",
                                                "url": tools_url, "toolCallTimeoutMs": 1_800_000,
                                                "failOnStartupError": True}}]})
        _clear(home / DSH_PATCH)
        # JSON is YAML, which dsh reads: a flow sequence, to which the transcripts' row adds its `!!js` root
        text = json.dumps(rows, indent=1)
        (home / DSH_PATCH).write_text(f"{text[:text.rindex(']')].rstrip()},\n {DSH_TRANSCRIPTS}\n]\n")

    @classmethod
    def web_search_args(cls, enabled: bool) -> list[str]:
        """dsh's web search and fetch are a row of its tree, set in the session's patch layer (prepare_home)."""
        return []

    def system_settings(self, system, prompt):
        """dsh takes no system prompt for one run, and its persona text (the system-prompt row) takes only the
        variables `{{provider}}` and `{{model}}`: any other `{{...}}`, which instructions may hold, fails the model's
        step. The instructions go at the top of the prompt instead, as a marked block."""
        return {}, f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{prompt}"

    @staticmethod
    def _reason(raw: str) -> dict:
        """The last `final` event's reason ({} when there is none: the run never finished)."""
        finals = [e for e in _objects(raw)[0] if e.get("type") == "final"]
        reason = finals[-1].get("reason") if finals else None
        return reason if isinstance(reason, dict) else {}

    @classmethod
    def served(cls, raw):
        """The run's last turn completed (its `final` reason)."""
        return cls._reason(raw).get("kind") == "completed"

    def outcome(self, raw, stream, last_file):
        """dsh exits 0 for a run whose `final` event is `completed`, and 1 for any other end (an error, an abort, the
        token limit, ...), but also 0 when SIGTERM stops it: a run without a completed end has failed, whatever its
        exit code. So has one whose tools did not start (an MCP row dsh reports inactive): it ran without them. The
        session started once dsh wrote its `session` event (a usage error is an `error` event before any session)."""
        notes, done = self.notices(raw), self.served(raw)
        down = DSH_TOOLS_DOWN.findall(notes)
        failure = f"{down[0][0]} did not start, so the session ran without its tools: {down[0][1]}" if down else \
            notes or "the run did not finish (no completed `final` event)"
        if "CONTEXT_WINDOW_EXCEEDED" in notes and self.settings.get("binding") and \
                not self.settings["binding"].get("context_window"):    # dsh's error (once), then what to do
            hint = ("; the backend declares no context_window, so dsh sized the model by its own catalog or, for a "
                    "model it does not know, as 1,000,000 tokens with 128,000 output tokens a request, which a smaller "
                    "server refuses: declare the backend's context_window")
            said = next(line for line in notes.splitlines() if "CONTEXT_WINDOW_EXCEEDED" in line)
            failure = said[:600 - len(hint)] + hint
        return {"reply": self.last_message(stream, last_file), "served": done,
                "unavailable": bool(UNAVAILABLE.search(notes)), "refused": False, "failed": not done or bool(down),
                "failure": failure[-600:], "started": any(e.get("type") == "session" for e in _objects(raw)[0])}

    @classmethod
    def errors(cls, event):
        """A run that did not complete: an error, as dsh writes it on stderr (`dsh: <code>: <message>`: the code is
        how a provider that could not serve is told, UNAVAILABLE), or any other end (aborted, max-tokens, blocked,
        interrupted, ...); a failure outside a turn."""
        kind = event.get("type")
        reason = event.get("reason") if isinstance(event.get("reason"), dict) else {}
        if kind == "final" and reason.get("kind") not in (None, "completed"):
            error = reason.get("error") if isinstance(reason.get("error"), dict) else {}
            if error.get("code"):
                return [f"dsh: {error['code']}: {error.get('message') or ''}"]
            return [str(error.get("message") or f"run {reason['kind']}")]
        if kind == "error":
            return [str(event.get("message") or "")]
        return []

    @classmethod
    def last_message(cls, log, last=None):
        """The `final` event's answer (dsh's last assistant text, never cut)."""
        text = ""
        for event in events(log):
            if event.get("type") == "final" and isinstance(event.get("text"), str):
                text = event["text"]
        return text

    @classmethod
    def parse(cls, log, model):
        return parse_dsh(log, model)

    @classmethod
    def steps(cls, stream):
        return dsh_steps(stream)

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        """The launcher's options first (the profile, the `--patch` layers: those in `args`, a deployment's, then
        the session's own, which is applied last and so wins), then the headless app's; dsh's launcher reads its
        own options only before the first one it does not know. Under workspace-write the `writable` paths are
        granted to dsh's own sandbox by their real paths, as it requires (an existing directory, no link)."""
        s, home = self.settings, last_file.parent                      # the session's home
        mode = s.get("permission_mode") or "workspace-write"
        if mode not in DSH_PERMISSION_MODES:
            raise ValueError(f"dsh: permission_mode is one of {DSH_PERMISSION_MODES}, not {mode!r}")
        added = []
        for path in s.get("writable") or () if mode == "workspace-write" else ():
            real = os.path.realpath(path)
            if not os.path.isdir(real):
                raise ValueError(f"dsh: a writable path is an existing directory, not {str(path)!r}")
            added += ["--add-dir", real]
        patches, rest = _dsh_args(s.get("args") or [])
        resume = _dsh_session(home, session) if session else None
        return [str(s.get("bin") or self.program), "--profile", "headless", *patches,
                "--patch", str(home / DSH_PATCH), "--json", "--permission-mode", mode,
                "--approval-policy", "never", *added,
                *(["--session-id", resume] if resume else []), *rest,
                *(["-"] if s.get("stdin") else ["--", prompt])]


def _dsh_args(args) -> tuple[list[str], list[str]]:
    """(the `--patch` layers among `args`, the other arguments): dsh's launcher takes a layer only before the app's
    options, so the layers go first, in their order."""
    args, patches, rest, i = [str(a) for a in args], [], [], 0
    while i < len(args):
        if args[i] == "--patch" and i + 1 < len(args):
            patches += args[i:i + 2]
            i += 2
            continue
        (patches if args[i].startswith("--patch=") else rest).append(args[i])
        i += 1
    return patches, rest


def _dsh_session(home: Path, handle) -> str | None:
    """Session `handle` when `home` keeps it (sessions/<its directory>/<id>/): dsh refuses an id it does not know, so
    a call whose session is gone starts a new one. dsh also resumes a session only in the directory it ran in, which
    an AgentEvolver's calls share (a resume elsewhere would fail rather than start over)."""
    if not re.fullmatch(r"session-[A-Za-z0-9-]+", str(handle)):
        return None
    root = Path(home) / "sessions"
    found = [p for p in root.glob(f"*/{handle}") if p.is_dir() and not p.is_symlink() and not p.parent.is_symlink()]
    return str(handle) if found else None


# Every CLI Base Agent, by each name make() takes for it.
CLIS: dict[str, type[CliAgent]] = {n: c for c in (Codex, Claude, Pi, Dsh) for n in (c.backend, *c.aliases)}


def _this_call(home: Path, handle: str, reported, resumed: bool):
    """This call's usage: Codex reports a thread's running total, so a resumed thread's report counts its earlier
    calls again. The last total of each thread is kept in the home (`usage/<thread>.json`) and taken off."""
    from hermit.agent.model.usage import Usage
    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
    ledger = home / "usage" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', handle)}.json"
    # The session could write its home: a link it left there (the ledger's directory or the ledger) is removed,
    # never followed, and the ledger is written without following one.
    _real_dir(ledger.parent)
    try:
        before = json.loads(ledger.read_text()) if resumed and _regular(ledger) else {}
        before = {f: int(before.get(f, 0)) for f in fields} if isinstance(before, dict) else {}
    except (OSError, ValueError, TypeError, OverflowError):     # OverflowError: a total of 1e1000
        before = {}
    if any(before.get(f, 0) > getattr(reported, f) or before.get(f, 0) < 0 for f in fields):
        before = {}                                 # no earlier total of this thread: the session wrote it
    _clear(ledger)                                  # a link or a pipe left there: removed, never written through
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({f_: getattr(reported, f_) for f_ in fields}))
    return Usage(reported.model, **{f: max(0, getattr(reported, f) - int(before.get(f, 0))) for f in fields},
                 calls=reported.calls, cost_usd=None if before else reported.cost_usd)


def _overlap(a: Path, b: Path) -> bool:
    a, b = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _target(m: Mount, workspace: Path) -> Path:
    """Where a mount is seen: its target, under the workspace when relative."""
    return Path(m.target) if Path(m.target).is_absolute() else workspace / m.target


_BWRAP_BINDS: dict[str, bool] = {}
_WARNED: set[str] = set()                      # what this process has warned of once


def _bwrap_binds() -> str | None:
    """bubblewrap, where it can make a mount namespace here (also inside a run's sandbox, which allows a nested one);
    else None. Asked once per process."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return None
    if bwrap not in _BWRAP_BINDS:
        try:
            _BWRAP_BINDS[bwrap] = subprocess.run([bwrap, "--dev-bind", "/", "/", "--", "true"], capture_output=True,
                                                 timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _BWRAP_BINDS[bwrap] = False
    return bwrap if _BWRAP_BINDS[bwrap] else None


def _bound(command: list[str], mounts: list[Mount], workspace: Path, hidden: list[Path] = ()) -> list[str]:
    """`command` in a mount namespace of its own in which each mount inside the workspace is its source bound at its
    target (read-only unless the mount says otherwise): a real directory, which `find`, `rg --files` and the CLI's
    own file tools descend into, as in a container; and each of `hidden` (the login's plan, which every session on the
    machine obeys and processes outside any sandbox write) an empty directory of its own, so no command of the session
    can change, lock or forge it. Everything else is as it was (`--dev-bind / /`: the same files, network and
    processes). Without a working bubblewrap the command is returned as it was, and the mounts are linked."""
    inside = [m for m in mounts if _elsewhere(m)]
    bwrap = _bwrap_binds() if inside or hidden else None
    if bwrap is None and hidden and os.environ.get("HA_ADAPTER_SANDBOX_ENFORCE") == "required":
        # In the task sandbox (its worker sets the run's enforce) this cover is all that keeps the session's commands
        # off the plan every session on the machine obeys: a run that requires its sandbox does not run without it.
        raise Unsupported(f"bubblewrap cannot make a mount namespace inside the task sandbox, so the login's plan "
                          f"({', '.join(map(str, hidden))}) cannot be kept from the session's commands; the run's "
                          "sandbox.enforce is required (environment.image runs each session in a container instead)")
    if bwrap is None:
        if inside and "linked" not in _WARNED:
            _WARNED.add("linked")
            print("hermit: bubblewrap cannot make a mount namespace here, so a task's data in the workspace is linked, "
                  "not bound: a search that walks the tree (`grep -r`, `find`, Claude Code's Glob and Grep, dsh's glob "
                  "and grep) does not follow a link",
                  file=sys.stderr, flush=True)
        if hidden and "plan" not in _WARNED:
            _WARNED.add("plan")
            print(f"hermit: bubblewrap cannot make a mount namespace here, so the session's commands can reach the "
                  f"login's plan directory ({', '.join(map(str, hidden))}); its files are never followed through a "
                  f"link, but a command can pause or lift the plan's holds", file=sys.stderr, flush=True)
        return command
    binds = [arg for path in hidden for arg in ("--tmpfs", str(path))]
    for m in inside:
        if m.target.is_symlink():                     # a link an earlier call (or the old way) left
            m.target.unlink()
        if Path(m.source).is_dir():
            m.target.mkdir(parents=True, exist_ok=True)
        else:
            m.target.parent.mkdir(parents=True, exist_ok=True)
            m.target.touch(exist_ok=True)
        binds += ["--ro-bind" if m.readonly else "--bind", str(m.source), str(m.target)]
    return [bwrap, "--die-with-parent", "--dev-bind", "/", "/", *binds, "--chdir", str(workspace), "--", *command]


def _elsewhere(m: Mount) -> bool:
    """Whether a mount is seen at another path than its source's: its target's folders resolved, not the target itself,
    which may be a link an earlier call left (to the source, or elsewhere)."""
    return m.target.parent.resolve() / m.target.name != Path(m.source).resolve()


def _is_bound(command: list[str], m: Mount) -> bool:
    """Whether `command` (from `_bound`) binds the mount at its target."""
    head = command[:command.index("--")] if "--" in command and Path(command[0]).name == "bwrap" else []
    return any(head[i] in ("--ro-bind", "--bind") and head[i + 2] == str(m.target) for i in range(len(head) - 2))


def _link(mounts: list[Mount]) -> None:
    """Without isolation, a mount inside the workspace is a link to its source (a link left from an earlier call is
    replaced)."""
    for m in mounts:
        if m.target.resolve() == Path(m.source).resolve():
            continue
        if m.target.is_symlink():
            m.target.unlink()
        if not m.target.exists():
            m.target.parent.mkdir(parents=True, exist_ok=True)
            m.target.symlink_to(m.source)
