"""`codex` and `claude`: a coding-agent CLI as a Base Agent (docs/framework/agent-layers.md#layer-2-base-agent).

One `run` is one session of the CLI in print mode: the prompt is given, the CLI works in `workspace` with its own
tools and the call's `tools` (served over MCP by the framework's tool server), and its last message is the reply.
The CLI's event stream is kept unchanged (`Result.log`) and read into the trajectory and the usage.

Settings (given once; `configured(...)` for a call's own):
  model             the CLI's model (absent: its default); with `binding`, the provider settings are in `args`/`env`
  binding           the resolved `<backend>/<model>` reference (hermit.agent.model.backends), for the key and price
  login             chatgpt: Codex with the user's ChatGPT sign-in (hermit.agent.base.login), from the copy kept
                    under `login_root` (default: the shared one)
  effort            Codex's model_reasoning_effort
  web_search        the CLI's web search, on or off (required: set either way)
  isolation         none: the CLI on this machine; sandbox: inside `sandbox` (the run's hermit.sandbox.Sandbox);
                    container: Codex in a Docker container of its own (`container`: image, network, proxy)
  codex_sandbox     Codex's own sandbox mode (default workspace-write)
  permission_mode   Claude Code's (default bypassPermissions)
  shell_env         Codex's shell_environment_policy.inherit for the session's commands (default core: no keys)
  project_doc_max_bytes   Codex's cap on AGENTS.md
  home              the CLI's home (CODEX_HOME, CLAUDE_CONFIG_DIR); kept between calls, so a session can be resumed.
                    Absent: a fresh directory per call
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
  Claude Code only: output_format (stream-json, or json: one result object), allowed_tools (its --tools),
  persist_session (false: --no-session-persistence), append_system_prompt, effort, permission_mode

Limits are enforced by the isolation, or the call is refused (Unsupported):

  isolation   readable                          hidden                         network
  none        linked into the workspace          refused                        refused (any list)
  sandbox     must be readable in the sandbox    covered (HidingSandbox)        the sandbox's own: [] only
  container   bound read-only at their target    all but workspace, home, data  the container's proxy: [] only
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Sequence

from hermit.agent.base import container, login as codex_login
from hermit.agent.base.interface import BaseAgent, Limits, Mount, Result, Unsupported
from hermit.agent.base.records import parse_log, price_usage
from hermit.agent.base.tool import Tool
from hermit.agent.base.trajectory import claude_steps, codex_steps, events

ISOLATIONS = ("none", "sandbox", "container")
# The CLI's own reports of a provider it could not reach (it retries those itself first), and of a container that
# could not start, not the session's text
UNAVAILABLE = re.compile(r"unexpected status (?:429|5\d\d)|exceeded retry limit|stream disconnected|"
                         r"error sending request|too many requests|service unavailable|bad gateway|overloaded|"
                         r"error response from daemon|cannot connect to the docker daemon|"
                         + re.escape(container.MISSING), re.I)
CODEX_HOME_SETTINGS = ["features.shell_snapshot = false",          # a snapshot would bring back the whole environment
                       "sandbox_workspace_write.network_access = false", 'approval_policy = "never"']
_ready: set = set()


def web_search_args(backend: str, enabled: bool) -> list[str]:
    """A CLI session's web search, set explicitly either way: Codex's hosted search (`web_search`, `live` or
    `disabled`), Claude Code's WebSearch and WebFetch tools (denied when off). The hosted search runs at the provider,
    so no sandbox, container or proxy can stop it: only this setting does."""
    if backend == "codex":
        return ["-c", f'web_search="{'live' if enabled else 'disabled'}"']
    return [] if enabled else ["--disallowedTools", "WebSearch,WebFetch"]


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
        lines.append('forced_login_method = "chatgpt"')
    if roles:
        lines.append("features.multi_agent = true")
    lines += ["", "[shell_environment_policy]", f'inherit = "{settings.get("shell_env") or "core"}"',
              'exclude = ["*KEY*", "*TOKEN*", "*SECRET*"]']
    if tools_url:
        lines += ["", f"[mcp_servers.{tools_name}]", f"url = {json.dumps(tools_url)}",
                  'default_tools_approval_mode = "approve"', "tool_timeout_sec = 1800"]
    (home / "agents").mkdir(parents=True, exist_ok=True)
    for role in roles:
        spec = "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in
                         {"name": role["name"], "description": role["description"],
                          "developer_instructions": role["instructions"]}.items()) + "\n"
        tomllib.loads(spec)
        (home / "agents" / f"{role['name']}.toml").write_text(spec, encoding="utf-8")
        lines += ["", f"[agents.{role['name']}]", f"description = {json.dumps(role['description'], ensure_ascii=False)}",
                  f"config_file = {json.dumps('agents/' + role['name'] + '.toml')}"]
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)
    (home / "config.toml").write_text(text, encoding="utf-8")


def last_message(backend: str, log: Path, last: Path | None = None) -> str:
    """The session's last message: Codex's `-o` file, else the last message in its stream; Claude Code's result."""
    if backend == "codex":
        if last is not None and last.is_file() and last.read_text(errors="replace").strip():
            return last.read_text(errors="replace")
        replies = [s.text for s in codex_steps(events(log)) if s.kind == "message"]
        return replies[-1] if replies else ""
    text = ""
    for event in events(log):
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            text = event["result"]
    return text


def served(backend: str, raw: str) -> bool:
    """Whether the provider served the session: it finished a turn."""
    marker = '"turn.completed"' if backend == "codex" else '"type":"result"'
    return marker in raw.replace(" ", "")


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
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    out, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


def _user_home(backend: str) -> Path:
    return codex_login.source().parent if backend == "codex" else Path.home() / ".claude"


class CliAgent(BaseAgent):
    backend = ""
    home_files: tuple[str, ...] = ()

    # ---------- what the call may do ----------
    def _isolation(self, workspace: Path, limits: Limits):
        """The isolation and its settings: (name, container settings or None, sandbox or None). Refuses limits the
        isolation cannot keep."""
        s = self.settings
        isolation = s.get("isolation") or "none"
        if isolation not in ISOLATIONS:
            raise ValueError(f"isolation must be one of {ISOLATIONS}, not {isolation!r}")
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
            if network is not None and (getattr(sandbox, "network", None) == "open" or getattr(sandbox, "allow", None)):
                raise Unsupported("the run's sandbox lets more than the model through (an open network or "
                                  "sandbox.allow): it cannot limit the network to the model")
            for m in limits.readable:
                if _target(m, workspace).resolve() != Path(m.source).resolve():
                    raise Unsupported(f"the run's sandbox shows data at its own path, not at {m.target}")
            if limits.hidden:
                from hermit.sandbox import HidingSandbox
                sandbox = HidingSandbox(sandbox, list(limits.hidden), keep=[m.source for m in limits.readable])
            return isolation, None, sandbox
        if self.backend != "codex":
            raise Unsupported("isolation: container runs Codex; Claude Code takes isolation none or sandbox")
        if s.get("stdin"):
            raise Unsupported("a container session takes its prompt on the command line, not on stdin")
        shown = [workspace, *(m.source for m in limits.readable)]         # every mount the container is given
        if s.get("home"):                         # a fresh home is a new directory: nothing hidden lies in it
            shown.append(Path(os.path.expanduser(str(s["home"]))))
        for path in limits.hidden:
            if any(Path(path).resolve().is_relative_to(Path(p).resolve()) for p in shown):
                raise Unsupported(f"{path} is to be hidden but lies in what the container is given")
        conf = container.settings(s)
        key = tuple(sorted(conf.items()))
        if key not in _ready:
            problems = container.check(conf)
            if problems:                                # else every session would fail
                raise RuntimeError("the agent's container is not ready: " + "; ".join(problems))
            _ready.add(key)
        return isolation, conf, None

    # ---------- the home ----------
    def _home(self) -> tuple[Path, bool]:
        s = self.settings
        if s.get("home"):
            home = Path(os.path.expanduser(str(s["home"]))).resolve()
            home.mkdir(parents=True, exist_ok=True)
            return home, False
        return Path(tempfile.mkdtemp(prefix=f"hermit-{self.backend}-")).resolve(), True

    def _sign_in(self, home: Path) -> list[Path]:
        """The session's sign-in in its home; the files to remove after the call."""
        s = self.settings
        if s.get("login") == "chatgpt":
            codex_login.prepare(Path(s.get("login_root") or codex_login.ROOT), home)
            return [home / codex_login.FILE]
        if s.get("binding"):
            return []
        copied = []
        for name in self.home_files if s.get("home_files") is None else s["home_files"]:
            source = _user_home(self.backend) / name
            if source.is_file():
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
        if workspace is None:
            raise Unsupported(f"{self.name} works in a workspace")
        if not isinstance(prompt, str):
            raise Unsupported(f"{self.name} takes a prompt, not chat messages")
        if not isinstance(self.settings.get("web_search"), bool):
            raise ValueError(f"{self.name}: set web_search, true or false")
        s, limits = self.settings, limits or Limits()
        workspace = Path(workspace).resolve()
        isolation, conf, sandbox = self._isolation(workspace, limits)
        home, fresh = self._home()
        if s.get("log"):
            stream = Path(s["log"])
        elif fresh:                                     # a fresh home goes after the call; the stream stays
            stream = Path(tempfile.mkstemp(prefix=f"agent-{self.backend}-", suffix=".jsonl")[1])
        else:
            stream = home / f"agent-{self.backend}.jsonl"
        stream.parent.mkdir(parents=True, exist_ok=True)
        last_file = home / "last_message.txt"
        last_file.unlink(missing_ok=True)
        timeout = limits.timeout_s or s.get("timeout_s", 3600)       # None: no clock (the caller cancels)
        timeout = float(timeout) if timeout else None
        mounts = [Mount(Path(m.source), _target(m, workspace), m.readonly) for m in limits.readable]
        server, copied, failed = None, [], True
        try:
            copied = self._sign_in(home)
            if tools:
                from hermit.agent.base.tool_server import ToolServer
                server = ToolServer(tools, host=container.gateway(conf) if conf else "127.0.0.1",
                                    mcp_only=s.get("mcp_tools")).start()
            name = s.get("tools_name") or "hermit_tools"
            own = {k: os.path.expandvars(str(v)) for k, v in (s.get("env") or {}).items()}   # $KEY_VAR -> the key
            if server is not None and s.get("tools_env"):
                own[s["tools_env"]] = server.url                  # the tools' JSON entrance, for the session's shell
            env = dict(own)
            if self.backend == "codex":
                write_home(home, s, roles, server.mcp_url if server else None, name)
                env["CODEX_HOME"] = str(home)
            else:
                env["CLAUDE_CONFIG_DIR"] = str(home)
            command = self.command(prompt, workspace, last_file, session=session, roles=roles,
                                   tools_url=server.mcp_url if server else None)
            started = time.time()
            if conf is not None:
                for m in mounts:
                    if m.target.is_relative_to(workspace):
                        m.target.mkdir(parents=True, exist_ok=True)   # the mount point, a real directory inside
                key_env = self._key_env()
                keys = {**env, **({key_env: os.environ.get(key_env, "")} if key_env else {})}
                code, timed_out, cancelled = container.run(command, conf=conf, work=workspace, home=home,
                                                           mounts=mounts, env=keys, timeout=timeout, stdout=stream,
                                                           cancel=s.get("cancel"))
            else:
                if isolation == "none":
                    _link(mounts)
                drop = set(s.get("env_drop") or ())
                full_env = {**{k: v for k, v in os.environ.items() if k not in drop}, **env}
                if sandbox is not None:
                    command, full_env = sandbox.wrap(command, writable=[home, *map(Path, s.get("writable") or ())],
                                                     env=full_env, home=home, keep=own,
                                                     forward=[server.port] if server else [],
                                                     readable=[m.source for m in mounts])
                    if (limits.hidden or limits.network is not None) and \
                            not any(Path(a).name == "bwrap" for a in command):
                        raise Unsupported("the run's sandbox did not isolate the call (no bubblewrap): it cannot hide "
                                          "paths or limit the network")
                code, timed_out, cancelled = run_process(command, env=full_env, cwd=workspace, stream=stream,
                                                         timeout=timeout, cancel=s.get("cancel"),
                                                         stdin=prompt if s.get("stdin") else None,
                                                         stderr=Path(s["stderr_log"]) if s.get("stderr_log") else None)
            wall_s = time.time() - started
            failed = False
        finally:
            if server is not None:
                server.close()
            if s.get("login") == "chatgpt":
                codex_login.keep_refreshed(Path(s.get("login_root") or codex_login.ROOT), home)
            for path in copied:
                path.unlink(missing_ok=True)                              # the sign-in does not outlive the call
            if failed and fresh:
                shutil.rmtree(home, ignore_errors=True)
        raw = stream.read_text(encoding="utf-8", errors="replace") if stream.is_file() else ""
        reply = last_message(self.backend, stream, last_file)
        handle, usage = parse_log(stream, self.backend, (s.get("binding") or {}).get("ref") or s.get("model")
                                  or self.backend)
        if self.backend == "codex" and handle and usage:
            usage = [_this_call(home, str(handle), usage[0], resumed=bool(session))]
        price_usage(usage, s)
        steps = (codex_steps if self.backend == "codex" else claude_steps)(events(stream))
        if fresh:
            shutil.rmtree(home, ignore_errors=True)
        return Result(reply=reply, trajectory=steps, usage=usage, session=handle, exit_code=code,
                      timed_out=timed_out, log=stream,
                      extra={"served": served(self.backend, raw), "unavailable": bool(UNAVAILABLE.search(raw)),
                             "started": any(line.lstrip().startswith("{") for line in raw.splitlines()),
                             "cancelled": cancelled, "isolation": isolation, "wall_s": round(wall_s, 1),
                             "output_tail": raw[-2000:]})

    def command(self, prompt: str, workspace: Path, last_file: Path, *, session=None, roles=(), tools_url=None) -> list:
        raise NotImplementedError


class Codex(CliAgent):
    name = backend = "codex"
    home_files = (codex_login.FILE,)

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        s = self.settings
        effort = s.get("effort")
        hooks = (workspace / ".codex" / "hooks.json").is_file()     # the deployment's hooks, trusted as deployed
        return [str(s.get("bin") or "codex"), "exec", "--json", "--skip-git-repo-check",
                "-C", str(s.get("cwd") or workspace),
                "-s", s.get("codex_sandbox") or "workspace-write", "-o", str(last_file),
                *(["-m", s["model"]] if s.get("model") else []),
                *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []),
                *(["--dangerously-bypass-hook-trust"] if hooks else []),
                *(["--enable", "multi_agent"] if roles else []), *(s.get("args") or []),
                *(["resume", str(session)] if session else []), *(["-"] if s.get("stdin") else [prompt])]


class Claude(CliAgent):
    name = backend = "claude"
    home_files = (".credentials.json",)

    def command(self, prompt, workspace, last_file, *, session=None, roles=(), tools_url=None):
        s = self.settings
        mcp = {"mcpServers": {s.get("tools_name") or "hermit_tools": {"type": "http", "url": tools_url}}}
        agents = {r["name"]: {"description": r["description"], "prompt": r["instructions"]} for r in roles}
        stream = (s.get("output_format") or "stream-json") == "stream-json"
        return [str(s.get("bin") or "claude"), "-p", *([] if s.get("stdin") else [prompt]),
                "--output-format", s.get("output_format") or "stream-json", *(["--verbose"] if stream else []),
                "--permission-mode", s.get("permission_mode") or "bypassPermissions",
                *(["--model", s["model"]] if s.get("model") else []),
                *(["--effort", s["effort"]] if s.get("effort") else []),
                *(["--tools", ",".join(s["allowed_tools"])] if s.get("allowed_tools") else []),
                *(["--no-session-persistence"] if s.get("persist_session") is False else []),
                *(["--append-system-prompt", s["append_system_prompt"]] if s.get("append_system_prompt") else []),
                *web_search_args("claude", bool(s.get("web_search"))),
                *(["--mcp-config", json.dumps(mcp)] if tools_url else []),
                *(["--agents", json.dumps(agents, ensure_ascii=False)] if agents else []),
                *(["--resume", str(session)] if session else []), *(s.get("args") or [])]


def _this_call(home: Path, handle: str, reported, resumed: bool):
    """This call's usage: Codex reports a thread's running total, so a resumed thread's report counts its earlier
    calls again. The last total of each thread is kept in the home (`usage/<thread>.json`) and taken off."""
    from hermit.agent.model.usage import Usage
    fields = ("input_tokens", "cached_input_tokens", "output_tokens")
    ledger = home / "usage" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', handle)}.json"
    before = json.loads(ledger.read_text()) if resumed and ledger.is_file() else {}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({f: getattr(reported, f) for f in fields}))
    return Usage(reported.model, **{f: max(0, getattr(reported, f) - int(before.get(f, 0))) for f in fields},
                 calls=reported.calls, cost_usd=None if before else reported.cost_usd)


def _target(m: Mount, workspace: Path) -> Path:
    """Where a mount is seen: its target, under the workspace when relative."""
    return Path(m.target) if Path(m.target).is_absolute() else workspace / m.target


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
