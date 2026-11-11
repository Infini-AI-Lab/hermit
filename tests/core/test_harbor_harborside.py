"""The Harbor-side half (hermit/harbor/harborside): the launch plan's executor (harborside.hermit) under every
HermitXXX, each class's install, the answer agent, the scripted agent and the stream driver's requests.

harbor is not importable from Hermit's venv, so the checks run under the Harbor venv's Python: this module is that
script too (`python test_harbor_harborside.py <check>[,<check>...] <scratch>` prints one JSON report per check), and
the tests share one such process (the `checks` fixture runs every check once); they skip when that venv is absent.
Stand-ins, at the edges only:

- FakeEnv: Harbor's environment as the agents see it, recording every upload, exec, compose call and download, and
  answering the probes the classes and Harbor's own installs make (the agent user, versions, the package manager, the
  image's own context file, mini-swe-agent's Python, a session that hangs until Harbor's timeout cancels it); a
  mounted binary's probe runs with bash, a stand-in in the binary's place (one only root may run refuses another
  user, and fails that user's `test -x`, as a mount of mode 700 does; one that warns prints Codex's warning before
  its version to a user other than root, whose HOME it cannot write); an exec may raise as one whose compose client
  this machine could not spawn.
- ShellEnv: the container's /installed-agent, /logs, /app and /opt (where a CLI's binary is mounted) mapped under the
  scratch directory and every command run with bash on this machine, so the generated launch script, the session's
  process group and its kill, the descriptor that carries a token, the copy-back and the files returned to the host
  run for real (no Docker). It refuses any command that names a path outside its root, answers an exec whose working
  directory is gone as Docker does (exit 126, runc's words), and may run with a PATH of its own (an image without
  base64 or wc, or whose cp or cat fails part-way, as on a full disk) and a HOME of its own (the agent user's, with an
  nvm) and in the C locale (where a test matches what bash says, in the same words whatever this machine's LANG).
- either with `noise`: other lines before and after every exec's output, as Harbor's docker exec merges stderr into
  stdout (ShellEnv's from a BASH_ENV of the image's that echoes).
- a stand-in CLI (a bash script first on the plan's PATH), and a stand-in chat endpoint and tool server on 127.0.0.1.

No Docker, no network, no model, no secret: the key and the token are fakes set in the check's own process."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
HARBORSIDE_ROOT = ROOT / "hermit" / "harbor"
HARBOR_VENV = Path(os.environ.get("HERMIT_HARBOR_VENV", "~/.cache/hermit/harbor-venv")).expanduser()
HARBOR_PYTHON = HARBOR_VENV / "bin" / "python"
FAKE_KEY = "fw-fake-key-never-real-0123456789"
FAKE_TOKEN = "sk-ant-oat01-fake-token-never-real"
INSTRUCTION = "Fix the failing test.\nIt's \"quoted\", $HOME stays text, and so does `this`.\n"
SUFFIX = "\n\n`hermit/demo/memory/NOTES.md` is your memory across questions."
IMAGE_CONTEXT = "# Project\n\nAlways run the tests before you stop.\n"
TOOL_PYTHON = "/root/.local/share/uv/tools/mini-swe-agent/bin/python"
# What Harbor's docker exec merges into a command's stdout besides its output: an echo of the image's BASH_ENV before
# it, a warning on stderr after it (the noisy environments' BASH_ENV prints both)
NOISE_BEFORE = "BASH_ENV of the image: hello there\n"
NOISE_AFTER = "WARN[0000] a compose warning after the output\n"
BASH_ENV_NOISE = f"echo \"{NOISE_BEFORE.strip()}\"\ntrap 'echo \"{NOISE_AFTER.strip()}\" >&2' EXIT\n"
# What Codex 0.164.0 says to --version before its version under a HOME it cannot write (HOME=/: a uid without a passwd
# entry, a home never created)
CODEX_HOME_WARNING = ("WARNING: proceeding, even though we could not create PATH aliases: Permission denied "
                      "(os error 13)")
CONTEXT_FILE = {"codex": "AGENTS.md", "claude": "CLAUDE.md", "pi": "AGENTS.md", "opencode": "AGENTS.md",
                "dsh": "AGENTS.md", "mini-swe-agent": None}
PROGRAM = {"codex": "codex", "claude": "claude", "pi": "pi", "opencode": "opencode", "dsh": "dsh",
           "mini-swe-agent": "@@HERMIT_PYTHON@@"}
CLASSES = {"codex": ("harborside.codex", "HermitCodex"), "claude": ("harborside.claude", "HermitClaude"),
           "pi": ("harborside.pi", "HermitPi"), "opencode": ("harborside.opencode", "HermitOpenCode"),
           "dsh": ("harborside.dsh", "HermitDsh"),
           "mini-swe-agent": ("harborside.mini_swe_agent", "HermitMiniSweAgent")}
# What each host writes beside the adapter's copy (hermit.hosts.<cli>), reduced to a file or two each.
HOST_FILES = {"codex": {".codex/hooks.json": '{"hooks": {}}', ".codex/config.toml": "# roles\n"},
              "claude": {".claude/settings.json": '{"hooks": {}}', ".mcp.json": '{"mcpServers": {}}'},
              "pi": {".pi/mcp.json": '{"mcpServers": {}}'}, "opencode": {"opencode.json": '{"mcp": {}}'},
              "dsh": {".dsh/hermit.patch.yml": "[]\n"}, "mini-swe-agent": {"mini-swe-agent.yaml": "agent: {}\n"}}
STUB_CLI = r"""#!/bin/bash
# A stand-in CLI: it reports what it was given, writes what a session writes, and may run on in the background.
printf 'argv:'; printf ' <%s>' "$@"; echo
echo "stdin: <$(cat)>"
echo "pwd: $PWD"
echo "hook-logs: $HERMIT_HOOK_LOGS"
echo "key-in-env: ${FIREWORKS_API_KEY:+yes}"
echo "renamed-key: ${OPENAI_API_KEY:+yes}"
echo "token-in-env: ${CLAUDE_CODE_OAUTH_TOKEN:+yes}"
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" ]; then
  echo "fd-sha: $(cat <&"$CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" | sha256sum | cut -c1-16)"
  echo "parent-environ-token: $(tr '\0' '\n' < /proc/$PPID/environ | grep -c '^CLAUDE_CODE_OAUTH_TOKEN=')"
fi
echo "pgid-is-own: $([ "$(ps -o pgid= -p $$ | tr -d ' ')" = "$$" ] && echo yes || echo no)"
mkdir -p hermit/demo/memory && echo "learned something" >> hermit/demo/memory/NOTES.md
echo "the answer" > "$CODEX_HOME/last_message.txt"
echo '{"refreshed": true}' > "$CODEX_HOME/auth.json"
if [ -n "$STUB_SLEEP" ]; then sleep "$STUB_SLEEP" & echo "$!" > "$HERMIT_HOOK_LOGS/child.pid"; wait; fi
exit "${STUB_EXIT:-0}"
"""
# A cp and a cat that fail part-way, as on a full disk (first on a ShellEnv's PATH): what they write is the first bytes
# of what they copy (cp's files mode 600), then they say so and exit 1. The cp fails only copying an adapter's memory,
# a last_message.txt or a sessions folder: a source of another name it copies as cp does
FAILING_CP = r"""#!/bin/bash
src="${@: -2:1}" dst="${@: -1}"
case "${src##*/}" in
  memory|last_message.txt|sessions) ;;
  *) command -p cp "$@"; exit ;;
esac
if [ -d "$src" ]; then
  mkdir -p "$dst" && for f in "$src"/*; do head -c 4 "$f" > "$dst/${f##*/}"; chmod 600 "$dst/${f##*/}"; done
else
  head -c 4 "$src" > "$dst" && chmod 600 "$dst"
fi
echo "cp: error writing '$dst': No space left on device" >&2
exit 1
"""
FAILING_CAT = r"""#!/bin/bash
head -c 8 "$1"
echo "cat: write error: No space left on device" >&2
exit 1
"""


def make_home(base: Path) -> Path:
    """A CLI home as hermit.harbor.launch renders it: a config naming the workdir by its placeholder, a sign-in."""
    home = base / "launch" / "home"
    if not home.is_dir():
        (home / "agents").mkdir(parents=True)
        (home / "config.toml").write_text('[projects."@@HERMIT_WORKDIR@@"]\ntrust_level = "trusted"\n')
        (home / "agents" / "reviewer.toml").write_text('name = "reviewer"\n')
        (home / "auth.json").write_text('{"tokens": "a sign-in, @@HERMIT_WORKDIR@@ left as it is"}')
    return home


def make_deploy(base: Path, cli: str, *, context: str = "Hermit context.\n") -> Path:
    """A staged deployment (hermit.harbor.staging): the adapter's copy, the host's files, the context, the record."""
    work = base / "deploy"
    if work.is_dir():
        return work
    (work / "hermit" / "demo" / "memory").mkdir(parents=True)
    (work / "hermit" / "demo" / "ADAPTER.md").write_text("The adapter.\n")
    (work / "hermit" / "demo" / "memory" / "NOTES.md").write_text("# Notes\n")
    for rel, text in HOST_FILES[cli].items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    if CONTEXT_FILE[cli]:
        (work / CONTEXT_FILE[cli]).write_text(context)
    (work / ".hermit-deploy.json").write_text(json.dumps({"host": cli, "adapter_id": "demo", "hooks": True}))
    return work


def make_bundle(base: Path) -> Path:
    """Hermit's modules for mini-swe-agent's runner, as the runner bundles them (reduced to the runner's file)."""
    (base / "hermit" / "agent" / "base").mkdir(parents=True, exist_ok=True)
    (base / "hermit" / "agent" / "base" / "mini_swe_agent_run.py").write_text("# Hermit's runner\n")
    return base


def write_plan(base: Path, cli: str, *, deploy=True, name="plan.json", **fields) -> Path:
    """A launch plan (schema version 1) for `cli` under `base`, with the fields given replacing the defaults."""
    base.mkdir(parents=True, exist_ok=True)
    home = make_home(base)
    work = make_deploy(base, cli) if deploy else None
    plan = {"version": 1, "cli": cli, "label": cli, "workdir": "/app",
            "deploy": {"work_dir": str(work), "adapter_id": "demo", "context_file": CONTEXT_FILE[cli],
                       "context_limit": None, "hooks": True, "copy_back": ["hermit/demo/memory"]} if work else None,
            "home": {"host_dir": str(home), "container_dir": "/installed-agent/hermit-home",
                     "secret_files": ["auth.json"]},
            "bundle": None, "command": [PROGRAM[cli], "exec", "-C", "@@HERMIT_WORKDIR@@", "@@HERMIT_PROMPT@@"],
            "stdin": False, "prompt_suffix": SUFFIX,
            "env": {"CODEX_HOME": "/installed-agent/hermit-home", "PROJECT": "@@HERMIT_WORKDIR@@/src"},
            "env_from_host": {}, "env_by_fd": {}, "nvm": False, "path_prepend": [], "stream": f"agent-{cli}.jsonl",
            "collect": [{"from": "/installed-agent/hermit-home/last_message.txt", "to": "last_message.txt"}],
            "return_files": [], "tools_script": None, "kill_grace_s": 2, "metadata": {"plan_digest": "abc123"}}
    plan.update(fields)
    path = base / name
    path.write_text(json.dumps(plan))
    return path


# ----- the Harbor side (runs under the Harbor venv) -----------------------------------------------------------------

def harbor_side(check: str, scratch: Path) -> dict:
    scratch = scratch / check                                 # one directory per check
    scratch.mkdir(parents=True, exist_ok=True)
    import asyncio
    import base64
    import hashlib
    import http.server
    import importlib
    import re
    import shlex
    import shutil
    import threading
    import time
    from types import SimpleNamespace
    from harbor.agents.installed.base import NonZeroAgentExitCodeError
    from harbor.environments.base import ExecResult
    from harbor.models.agent.context import AgentContext
    from harborside import hermit as executor

    os.environ["FIREWORKS_API_KEY"] = FAKE_KEY                # Harbor's environment holds the secrets by value
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = FAKE_TOKEN

    def agent_class(cli: str):
        module, name = CLASSES[cli]
        return getattr(importlib.import_module(module), name)

    def make(cli: str, plan: Path | None = None, **kwargs):
        logs = scratch / "logs"
        logs.mkdir(exist_ok=True)
        return agent_class(cli)(logs_dir=logs, **({"plan": str(plan)} if plan else {}), **kwargs)

    class FakeEnv:
        """Records every call; answers the probes. `files` are the container's files `test -f` and `base64` find."""

        def __init__(self, *, workdir="/app", image_context="", python3=True, missing=(), launch_code=0, hang=False,
                     mounted=None, tool_python=TOOL_PYTHON, bundle_ok=True, files=None, misses=0, listing="",
                     failing=(), answers=None, pwd="/app", noise=False, hanging=(), hang_s=3600, hanging_once=(),
                     root_only=(), warns=(), raising=None):
            self.calls, self.default_user, self.pwd = [], None, pwd
            self.task_env_config = SimpleNamespace(workdir=workdir)
            self.image_context, self.python3, self.missing = image_context, python3, set(missing)
            self.launch_code, self.hang, self.running = launch_code, hang, False
            # commands holding one of these fail, saying what the mapping gives (given names alone: a full disk)
            self.failing = dict(failing) if isinstance(failing, dict) else \
                dict.fromkeys(failing, "cp: cannot create directory: No space left on device\n")
            # ... and these exit as the mapping gives (code, output): an exec that exits 0 without what it prints
            self.answers = dict(answers or {})
            self.hanging, self.hang_s = tuple(hanging), hang_s   # ... hang until Harbor's timeout (or for hang_s)
            self.hanging_once = list(hanging_once)            # ... the first such exec only (the deadline landed in it)
            # ... and these raise OSError(errno) from the exec itself: its compose client not spawned (EMFILE, EAGAIN)
            self.raising = dict(raising or {})
            self.mounted = dict(mounted or {})                # a mounted binary's path -> what its --version says
            self.root_only = set(root_only)                   # ... those only root may run (mode 700): another user's
                                                              # probe exits 126 and its `test -x` 1 (no user: the
                                                              # image's own, root)
            self.warns = set(warns)                           # ... those that, run by a user other than root (whose
                                                              # HOME they cannot write), warn before their version
            self.tool_python, self.bundle_ok = tool_python, bundle_ok
            self.files, self.misses, self.listing = dict(files or {}), misses, listing
            self.noise = noise                                # every exec's output between NOISE_BEFORE and _AFTER

        def _merge_env(self, env):
            return dict(env or {})

        def _resolve_user(self, user):
            return user if user is not None else self.default_user

        def _output_callback(self):
            return None

        async def _run_docker_compose_command(self, command, check=True, timeout_sec=None, stdin_data=None,
                                              on_output=None):
            names = [command[i + 1] for i, a in enumerate(command) if a == "-e" and "=" not in command[i + 1]]
            self.calls.append(("compose", list(command), {n: bool(os.environ.get(n)) for n in names}))
            return await self._answer(command[-1], None)

        async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
            command = command.removeprefix("set -o pipefail; ")     # BaseInstalledAgent._exec's prefix
            self.calls.append(("exec", user, command, dict(env or {})))
            result = await self._answer(command, user)
            if self.noise:
                result = ExecResult(stdout=NOISE_BEFORE + (result.stdout or "") + NOISE_AFTER, stderr=result.stderr,
                                    return_code=result.return_code)
            return result

        async def _answer(self, c, user):
            def ok(out=""):
                return ExecResult(stdout=out, stderr="", return_code=0)

            def code(n, out=""):
                return ExecResult(stdout=out, stderr="", return_code=n)

            def b64(data) -> str:
                return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()

            if any(part in c for part in self.hanging):
                await asyncio.sleep(self.hang_s)
            for part in [p for p in self.hanging_once if p in c]:
                self.hanging_once.remove(part)             # run again, it answers
                await asyncio.sleep(self.hang_s)
            for part, number in self.raising.items():
                if part in c:
                    raise OSError(number, os.strerror(number))
            for part, said in self.failing.items():
                if part in c:
                    return code(1, said)
            for part, (n, said) in self.answers.items():
                if part in c:
                    return code(n, said)
            if c.startswith('echo "hermit-owner:$(id -u):$(id -g)"'):
                return ok("hermit-owner:1000:1000\n")
            if c == 'echo "hermit-pwd:$(pwd)"':                # where Harbor's execs start: the image's WORKDIR
                return ok(f"hermit-pwd:{self.pwd}\n")
            if "/installed-agent/hermit/original-" in c and c.startswith("mkdir -p"):   # the context's capture
                return ok(f"hermit-context:{b64(self.image_context)}\n")           # empty: the image has none
            for binary, said in self.mounted.items():
                other = self._resolve_user(user) not in (None, "root", 0)
                if c == f"test -x {shlex.quote(binary)}":   # whether the exec's user may execute the mount
                    return code(1 if other and binary in self.root_only else 0)
                if f"{binary} --version" in c:             # the mount's probe, with bash: a stand-in says `said`
                    stand_in = f"printf %s {shlex.quote(said)}" if said else "(exit 127)"
                    if other and binary in self.warns:
                        stand_in = f"{{ echo {shlex.quote(CODEX_HOME_WARNING)} >&2; {stand_in}; }}"
                    if other and binary in self.root_only:
                        refused = shlex.quote(f"bash: {binary}: Permission denied")
                        stand_in = f"{{ echo {refused} >&2; (exit 126); }}"
                    got = subprocess.run(["bash", "-c", c.replace(f"{binary} --version", stand_in)], text=True,
                                         capture_output=True, env={"PATH": os.environ["PATH"]}, timeout=60)
                    return code(got.returncode, got.stdout)
            if "hermit-data:" in c:                        # an attachment's bytes, in base64
                path = c.split("base64 < ", 1)[1].split(" |")[0].strip()
                return ok(f"hermit-data:{b64(self.files.get(path, b''))}\n")
            if re.search(r"(codex|claude|pi|opencode|dsh) --version$", c) and "npm" not in c and "curl" not in c:
                return code(127)                           # Harbor's probe of an installed CLI: none yet
            if c.startswith("command -v") and "||" in c:    # any of them (OpenCode's downloaders)
                return code(0 if any(n not in self.missing for n in re.findall(r"command -v (\S+)", c)) else 1)
            if c.startswith("command -v python3"):
                return code(0 if self.python3 else 1)
            if c.startswith("command -v"):
                return code(0 if all(n not in self.missing for n in re.findall(r"command -v (\S+)", c)) else 1)
            if c.startswith("for manager in apt-get"):
                return ok("apt-get")
            if "uv tool dir" in c:
                return code(0, f"hermit-python:{self.tool_python}\n") if self.tool_python else code(1)
            if "import hermit.agent.base.mini_swe_agent_run" in c:
                return code(0, "") if self.bundle_ok else code(1, "ModuleNotFoundError: No module named 'yaml'\n")
            if c.startswith("test -f "):
                path = c.split()[2].strip("'")
                if executor.RETURN_LINE in c:              # the return's check: whether the session left the file
                    return ok(f"{executor.RETURN_LINE}{'yes' if path in self.files else 'no'}\n")
                if path not in self.files:
                    return code(1)
                if self.misses > 0 and "base64" in c:      # the task's entrypoint has not written it yet
                    self.misses -= 1
                    return code(1)
                return ok(f"hermit-instruction:{b64(self.files[path])}\n" if "base64" in c else "")
            if c.startswith("shopt -s nullglob"):
                return ok("".join(f"hermit-file:{path}\n" for path in self.listing.splitlines()))
            if c.startswith("n=$(wc -c < "):
                return ok(f"hermit-size:{len(self.files.get(c.split('< ', 1)[1].split(')')[0].strip(), b''))}\n")
            if c.endswith("bash /installed-agent/hermit/run/launch.sh"):
                if self.hang:
                    self.running = True
                    await asyncio.sleep(3600)              # until Harbor's timeout cancels the agent
                return code(self.launch_code)
            if "session.pid" in c and "kill -TERM" in c:
                killed, self.running = self.running, False
                return ok("hermit-killed\n" if killed else "")
            return ok()

        async def upload_file(self, source_path, target_path):
            self.calls.append(("upload_file", target_path, Path(source_path).read_bytes().decode("utf-8", "replace")))

        async def upload_dir(self, source_dir, target_dir):
            files = {}                                     # as `docker cp` takes them: a link as the link it is
            for p in sorted(Path(source_dir).rglob("*")):
                if p.is_symlink():
                    files[p.relative_to(source_dir).as_posix()] = f"<link to {os.readlink(p)}>"
                elif p.is_file():
                    files[p.relative_to(source_dir).as_posix()] = p.read_bytes().decode("utf-8", "replace")
            self.calls.append(("upload_dir", target_dir, files))

        async def download_file(self, source_path, target_path):
            self.calls.append(("download_file", source_path, str(target_path)))
            Path(target_path).write_text(f"downloaded {source_path}")

        async def download_dir(self, source_dir, target_dir):
            self.calls.append(("download_dir", source_dir, str(target_dir)))

        async def is_dir(self, path, user=None):
            return True

        async def is_file(self, path, user=None):
            return path in self.files

    class PlainEnv(FakeEnv):
        """An environment that cannot pass a variable by name (not a local Docker one)."""
        _run_docker_compose_command = None

    class ShellEnv:
        """The container's paths under `root`, its commands run here with bash (the module's docstring)."""
        PREFIXES = re.compile(r"(?<![\w./-])(/installed-agent|/logs|/app|/opt)(?![\w.-])")
        # A path starts a word (or follows a redirection, an assignment, an opening quote); "$p"/status continues one.
        PATHS = re.compile(r"""(?:^|(?<=[\s=<>(;|&'])|(?<=[\s=<>(]"))/[^\s"';|&()<>]*""", re.M)
        SAFE = ("/dev/null", "/proc/")

        def __init__(self, root: Path, workdir="/app", noise=False, path=None, locale=None, home=None):
            self.root, self.calls, self.procs, self.default_user = root, [], [], None
            for d in ("installed-agent", "logs/agent", "app"):
                (root / d).mkdir(parents=True, exist_ok=True)
            self.task_env_config = SimpleNamespace(workdir=workdir)
            self.path = path                               # the container's PATH, when not this machine's
            self.home = home                               # the agent user's HOME, when not this machine's
            self.locale = locale                           # its LC_ALL ("C": bash's own words, whatever the LANG)
            self.bash_env = None                           # noise: every bash prints NOISE_BEFORE and NOISE_AFTER
            if noise:
                self.bash_env = root / "bash_env.sh"
                self.bash_env.write_text(BASH_ENV_NOISE)

        def map(self, text: str) -> str:
            return self.PREFIXES.sub(lambda m: str(self.root) + m.group(1), str(text))

        def _check(self, command: str) -> None:
            for path in self.PATHS.findall(command):
                if not (path.startswith(str(self.root)) or path.startswith(self.SAFE)):
                    raise AssertionError(f"ShellEnv refuses a command naming {path} outside its root: {command[:300]}")

        def _merge_env(self, env):
            return dict(env or {})

        def _resolve_user(self, user):
            return user if user is not None else self.default_user

        def _output_callback(self):
            return None

        async def _bash(self, command, env, cwd, names=()):
            mapped = self.map(command)
            self._check(mapped)
            full = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "USER", "LOGNAME")}
            if self.path is not None:
                full["PATH"] = self.path
            if self.home is not None:
                full["HOME"] = str(self.home)
            if self.locale is not None:
                full["LC_ALL"] = self.locale
            full.update({k: self.map(v) for k, v in env.items()})
            full.update({n: os.environ[n] for n in names if n in os.environ})        # by name, as compose does
            if self.bash_env is not None:
                full["BASH_ENV"] = str(self.bash_env)
            where = cwd or self.task_env_config.workdir
            if not Path(self.map(where)).is_dir():         # as Docker refuses it: runc cannot enter a workdir gone
                return ExecResult(stdout=f"OCI runtime exec failed: exec failed: unable to start container process: "
                                         f'chdir to cwd ("{where}") set in config.json failed: no such file or '
                                         "directory: unknown\n", stderr="", return_code=126)
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", mapped, cwd=self.map(where), env=full,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            self.procs.append(proc)
            out, _ = await proc.communicate()              # if cancelled, the process goes on, as docker exec's does
            return ExecResult(stdout=out.decode(errors="replace"), stderr="", return_code=proc.returncode)

        async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
            command = command.removeprefix("set -o pipefail; ")
            self.calls.append(("exec", user, command, dict(env or {})))
            return await self._bash(command, dict(env or {}), cwd)

        async def _run_docker_compose_command(self, command, check=True, timeout_sec=None, stdin_data=None,
                                              on_output=None):
            self.calls.append(("compose", list(command), {}))
            args, env, names, cwd = list(command[1:]), {}, [], None
            while args and args[0] != "main":
                flag, value = args.pop(0), args.pop(0)
                if flag == "-w":
                    cwd = value
                elif flag == "-e" and "=" in value:
                    env[value.split("=", 1)[0]] = value.split("=", 1)[1]
                elif flag == "-e":
                    names.append(value)
            return await self._bash(args[-1], env, cwd, names)

        async def upload_file(self, source_path, target_path):
            target = Path(self.map(target_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            data = Path(source_path).read_bytes()
            self.calls.append(("upload_file", target_path, data.decode("utf-8", "replace")))
            target.write_bytes(self.map(data.decode()).encode() if target.name == "launch.sh" else data)

        async def upload_dir(self, source_dir, target_dir):
            self.calls.append(("upload_dir", target_dir, {}))
            shutil.copytree(source_dir, self.map(target_dir), dirs_exist_ok=True)

        async def download_file(self, source_path, target_path):
            self.calls.append(("download_file", source_path, str(target_path)))
            shutil.copy(self.map(source_path), target_path)

        async def settle(self):
            for proc in self.procs:
                if proc.returncode is None:
                    await asyncio.wait_for(proc.wait(), 30)

    def calls(env, kind):
        return [c for c in env.calls if c[0] == kind]

    def uploaded(env) -> dict:
        """target -> text, for every file uploaded (a directory's files under it)."""
        out = {}
        for c in env.calls:
            if c[0] == "upload_file":
                out[c[1]] = c[2]
            elif c[0] == "upload_dir":
                out.update({f"{c[1]}/{rel}": text for rel, text in c[2].items()})
        return out

    def leaks(env) -> list:
        """Every recorded call (exec, upload, compose, download) that holds the key's or the token's value."""
        return [json.dumps(c)[:300] for c in env.calls if FAKE_KEY in json.dumps(c) or FAKE_TOKEN in json.dumps(c)]

    def order(env) -> list:
        return [c[0] + ":" + (c[2][:60] if c[0] == "exec" else c[1][-1][:60] if c[0] == "compose" else c[1])
                for c in env.calls]

    def run(agent, env, instruction=INSTRUCTION, timeout=None, cancel_when=None):
        """The agent's run as Harbor calls it, under its timeout if any, or cancelled (what that timeout does to the
        run) once `cancel_when()` holds; the error raised, or None."""
        async def go():
            task = asyncio.ensure_future(agent.run(instruction, env, AgentContext()))
            try:
                if cancel_when is not None:
                    deadline = time.monotonic() + 60
                    while not cancel_when() and not task.done() and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    task.cancel()
                await (task if timeout is None else asyncio.wait_for(task, timeout))
            finally:
                if isinstance(env, ShellEnv):
                    await env.settle()
        try:
            asyncio.run(go())
            return None
        except BaseException as exc:                       # noqa: BLE001 - reported to the test
            return [type(exc).__name__, str(exc)]

    def metadata(agent) -> dict:
        context = AgentContext()
        agent.populate_context_post_run(context)
        return context.model_dump()

    report: dict = {}

    if check == "codex_plan":
        plan = write_plan(scratch, "codex", env_from_host={"FIREWORKS_API_KEY": "FIREWORKS_API_KEY",
                                                           "OPENAI_API_KEY": "FIREWORKS_API_KEY"},
                          path_prepend=["/opt/hermit-codex/bin"],
                          collect=[{"from": "/installed-agent/hermit-home/last_message.txt", "to": "last_message.txt"},
                                   {"from": "/installed-agent/hermit-home/auth.json", "to": "auth.json"}],
                          return_files=[{"from": "/installed-agent/hermit-home/auth.json",
                                         "to": str(scratch / "return" / "auth.json")}])
        canary = scratch / "host-file.txt"                 # a file of this host that links in the staging point at
        canary.write_text("a host file, @@HERMIT_WORKDIR@@\n")
        (scratch / "deploy" / ".codex" / "linked.txt").symlink_to(canary)
        (scratch / "deploy" / "linked-dir").symlink_to(scratch)
        (scratch / "launch" / "home" / "linked.toml").symlink_to(canary)
        report["canary"], report["scratch"] = str(canary), str(scratch)
        env = FakeEnv(image_context=IMAGE_CONTEXT, files={"/installed-agent/hermit-home/auth.json": "{}"})
        agent = make("codex", plan)
        report["raised"] = run(agent, env)
        report["order"] = order(env)
        report["uploaded"] = uploaded(env)
        report["execs"] = [(c[1], c[2]) for c in calls(env, "exec")]
        report["compose"] = [c[1] for c in calls(env, "compose")]
        report["compose_names_resolved"] = [c[2] for c in calls(env, "compose")]
        report["leaks"] = leaks(env)
        report["returned"] = (scratch / "return" / "auth.json").read_text()
        report["canary_after"] = canary.read_text()
        report["context"] = metadata(agent)

    elif check == "context_limit":
        for name, limit in (("over", 60), ("within", 4096)):
            plan = write_plan(scratch / name, "dsh")
            data = json.loads(plan.read_text())
            data["deploy"]["context_limit"] = limit
            plan.write_text(json.dumps(data))
            env = FakeEnv(image_context=IMAGE_CONTEXT)
            report[name] = {"raised": run(make("dsh", plan), env), "uploaded": uploaded(env), "order": order(env)}

    elif check == "every_cli":
        bundle = make_bundle(scratch / "bundle")
        for cli in CLASSES:
            extra = {"bundle": {"host_dir": str(bundle), "container_dir": "/installed-agent/bundle"}} \
                if cli == "mini-swe-agent" else {}
            plan = write_plan(scratch / cli, cli, stdin=cli in ("claude", "opencode", "mini-swe-agent"), **extra)
            env = FakeEnv(image_context=IMAGE_CONTEXT)
            raised = run(make(cli, plan), env)
            files = uploaded(env)
            report[cli] = {"raised": raised, "record": files.get("/app/.hermit-deploy.json"),
                           "launch": files.get("/installed-agent/hermit/run/launch.sh"),
                           "prompt": files.get("/installed-agent/hermit/run/prompt.txt"),
                           "context": files.get(f"/app/{CONTEXT_FILE[cli]}") if CONTEXT_FILE[cli] else None,
                           "workdir_files": sorted(k for k in files if k.startswith("/app/"))}

    elif check == "workdir_pwd":
        plan = write_plan(scratch, "codex", workdir=None)
        env = FakeEnv(workdir=None, pwd="/testbed")
        report["raised"] = run(make("codex", plan), env)
        report["uploaded"] = uploaded(env)
        report["pwd_user"] = [c[1] for c in calls(env, "exec") if c[2] == 'echo "hermit-pwd:$(pwd)"']
        # ... and an image with no WORKDIR either: Harbor's execs start in /
        env = FakeEnv(workdir=None, pwd="/")
        report["root_raised"] = run(make("codex", write_plan(scratch / "root", "codex", workdir=None)), env)
        report["root_dirs"] = [c[1] for c in calls(env, "upload_dir")]
        report["root_files"] = sorted(f"{c[1]} {c[2]}" for c in calls(env, "upload_file"))

    elif check == "shell_noise":
        # Harbor's docker exec merges stderr into stdout: every value read from the container (the agent user, the
        # workdir, the image's own context file) comes with other lines before and after it, for real with bash
        env = ShellEnv(scratch / "c", noise=True)
        fakebin = env.root / "installed-agent" / "fakebin"
        fakebin.mkdir(parents=True)
        (fakebin / "codex").write_text(STUB_CLI)
        (fakebin / "codex").chmod(0o755)
        (env.root / "app" / "AGENTS.md").write_text(IMAGE_CONTEXT)                # the image's own context file
        plan = write_plan(scratch, "codex", workdir=None, path_prepend=["/installed-agent/fakebin"])
        report["raised"] = run(make("codex", plan), env, timeout=120)
        report["root"], report["owner"] = str(env.root), f"{os.getuid()}:{os.getgid()}"
        report["chowns"] = [c[2] for c in calls(env, "exec") if c[2].startswith("chown")]
        report["context"] = (env.root / "app" / "AGENTS.md").read_text()
        report["launch"] = next((c[2] for c in calls(env, "upload_file") if c[1].endswith("/launch.sh")), None)
        stream = env.root / "logs" / "agent" / "agent-codex.jsonl"
        report["stream"] = stream.read_text() if stream.is_file() else None

    elif check == "slash_prompt":
        for name, prefix in (("guarded", "\n"), ("plain", None)):
            plan = write_plan(scratch / name, "claude", deploy=False, stdin=True, prompt_slash_prefix=prefix)
            env = FakeEnv()
            report[name] = {"raised": run(make("claude", plan), env, instruction="/app/data.csv holds the rows."),
                            "prompt": uploaded(env).get("/installed-agent/hermit/run/prompt.txt")}

    elif check == "stream_steps":
        first = write_plan(scratch / "one", "codex")
        make_deploy(scratch / "two", "codex", context="Hermit context, revised.\n")
        (scratch / "two" / "deploy" / ".codex" / "hooks.json").unlink()            # this step deploys no hooks
        second = write_plan(scratch / "two", "codex")
        third = write_plan(scratch / "three", "codex", deploy=False)
        env = FakeEnv(image_context=IMAGE_CONTEXT)
        agent = make("codex", first)
        for name, plan in (("one", first), ("two", second), ("three", third)):
            agent.set_plan(str(plan))
            env.calls.clear()
            report[name] = {"raised": run(agent, env), "uploaded": uploaded(env),
                            "root_execs": [c[2] for c in calls(env, "exec") if c[1] == "root"]}

    elif check == "deploy_failed_step":
        # One container, two steps: the first deploys an adapter whose hooks are active; the second deploys none, and
        # the removal of what the first one deployed fails
        env = FakeEnv(image_context=IMAGE_CONTEXT)
        agent = make("codex", write_plan(scratch / "one", "codex"))
        report["one"] = {"raised": run(agent, env), "metadata": metadata(agent)["metadata"]}
        agent.set_plan(str(write_plan(scratch / "two", "codex", deploy=False)))
        env.failing = {"rm -rf /app/.codex": "rm: cannot remove '/app/.codex/hooks.json': Device or resource busy\n"}
        report["two"] = {"raised": run(agent, env), "metadata": metadata(agent)["metadata"]}

    elif check in ("cancel_fake", "nonzero_exit"):
        plan = write_plan(scratch, "pi", deploy=check == "nonzero_exit", nvm=True)
        env = FakeEnv(hang=check == "cancel_fake", launch_code=3,
                      failing=("hermit-copy",) if check == "nonzero_exit" else ())
        agent = make("pi", plan)
        report["raised"] = run(agent, env, timeout=1.5 if check == "cancel_fake" else None)
        report["order"] = order(env)
        report["kill"] = [(c[1], c[2]) for c in calls(env, "exec") if "kill -TERM" in c[2]]
        report["metadata"] = metadata(agent)["metadata"]

    elif check == "launch_failures":
        # harborside's own steps before the session's exec failing (the deployment's chown, the probe of the task's
        # agent user), and a CLI that exits 97 itself (no launcher's words with it)
        # (and an image without base64, whose context file must not be taken for none and overwritten)
        for name, failing, exit_code in (("chown", ("chown -R 1000:1000 /app/hermit/demo",), 0),
                                         ("owner", ("id -u",), 0), ("own_97", (), 97),
                                         ("no_base64", ("command -v base64",), 0)):
            env = FakeEnv(failing=failing, launch_code=exit_code, image_context=IMAGE_CONTEXT)
            report[name] = {"raised": run(make("codex", write_plan(scratch / name, "codex")), env),
                            "order": order(env)}
        # the launch script cannot enter the workdir, for real with bash: its exit 97 and its own words
        env = ShellEnv(scratch / "c")
        plan = write_plan(scratch / "enter", "codex", deploy=False, workdir="/app/missing")
        report["cannot_enter"] = {"raised": run(make("codex", plan), env, timeout=120), "order": order(env)}
        # the image's own context file whose read fails, for real with bash (a base64 first on the container's PATH
        # that fails reading it); a stand-in CLI first on the plan's PATH, were the session to start
        failing_bin = scratch / "failing-base64-bin"
        failing_bin.mkdir()
        (failing_bin / "base64").write_text("#!/bin/sh\necho 'base64: read error: Input/output error' >&2\nexit 1\n")
        (failing_bin / "base64").chmod(0o755)
        env = ShellEnv(scratch / "read-c", path=f"{failing_bin}:{os.environ['PATH']}")
        (env.root / "app" / "AGENTS.md").write_text(IMAGE_CONTEXT)
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text("#!/bin/bash\nexit 0\n")
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        plan = write_plan(scratch / "read_fails", "codex", path_prepend=["/installed-agent/fakebin"])
        report["read_fails"] = {"raised": run(make("codex", plan), env, timeout=120), "order": order(env),
                                "context": (env.root / "app" / "AGENTS.md").read_text()}

    elif check == "capture_fails":
        # For real with bash, one container and three steps of a stream: the first step's capture of the image's own
        # AGENTS.md meets a cat first on the container's PATH that fails part-way (FAILING_CAT); the second runs with
        # the container's cat working again; the third deploys nothing, so the image's own file is put back. A
        # stand-in CLI first on the plan's PATH, were a session to start.
        failing_bin = scratch / "failing-cat-bin"
        failing_bin.mkdir()
        (failing_bin / "cat").write_text(FAILING_CAT)
        (failing_bin / "cat").chmod(0o755)
        env = ShellEnv(scratch / "c")
        (env.root / "app" / "AGENTS.md").write_text(IMAGE_CONTEXT)
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text("#!/bin/bash\nexit 0\n")
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        deployed = write_plan(scratch / "deployed", "codex", path_prepend=["/installed-agent/fakebin"])
        bare = write_plan(scratch / "bare", "codex", deploy=False, path_prepend=["/installed-agent/fakebin"])
        agent = make("codex", deployed)
        kept = env.root / "installed-agent" / "hermit" / "original-AGENTS.md"
        for name, plan, path in (("failed", deployed, f"{failing_bin}:{os.environ['PATH']}"),
                                 ("again", deployed, None), ("restored", bare, None)):
            agent.set_plan(str(plan))
            env.path = path
            env.calls.clear()
            report[name] = {"raised": run(agent, env, timeout=120),
                            "launched": any(step.startswith("exec:exec bash") for step in order(env)),
                            "context": (env.root / "app" / "AGENTS.md").read_text(),
                            "kept": kept.read_text() if kept.is_file() else None}

    elif check == "launch_exec_failed":
        # The session's exec failing in Docker before the launch script runs: runc could not start it (the host's /tmp
        # full: exit 126 and runc's words, the script not run; every other command for real with bash), and compose's
        # service had stopped (the probe of the stream fails as well). Then, for real with bash, a launch script that
        # ran a stand-in CLI which exits 3 at once and prints nothing: its stream is there, empty.
        oci = ("OCI runtime exec failed: exec failed: unable to start container process: open /tmp/runc-process1: no "
               "space left on device: unknown\n")

        class RuncFailedEnv(ShellEnv):
            """ShellEnv whose launch exec fails as Docker's does when runc cannot start it: the script never runs
            (once the first `runs` launches have run for real)."""
            runs = 0

            async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
                if command.endswith("bash /installed-agent/hermit/run/launch.sh") and self.runs > 0:
                    self.runs -= 1
                elif command.endswith("bash /installed-agent/hermit/run/launch.sh"):
                    self.calls.append(("exec", user, command, dict(env or {})))
                    return ExecResult(stdout=oci, stderr=None, return_code=126)
                return await super().exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)
        env = RuncFailedEnv(scratch / "runc-c")
        plan = write_plan(scratch / "runc", "codex", deploy=False)
        report["runc"] = {"raised": run(make("codex", plan), env, timeout=120), "order": order(env),
                          "stream": (env.root / "logs" / "agent" / "agent-codex.jsonl").exists()}
        stopped = 'service "main" is not running\n'
        env = FakeEnv(failing={"bash /installed-agent/hermit/run/launch.sh": stopped,
                               "test -e /logs/agent/agent-codex.jsonl": stopped})
        report["stopped"] = {"raised": run(make("codex", write_plan(scratch / "stopped", "codex", deploy=False)), env),
                             "order": order(env)}
        env = ShellEnv(scratch / "empty-c")
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text("#!/bin/bash\nexit 3\n")
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        plan = write_plan(scratch / "empty", "codex", deploy=False, path_prepend=["/installed-agent/fakebin"])
        stream = env.root / "logs" / "agent" / "agent-codex.jsonl"
        report["empty"] = {"raised": run(make("codex", plan), env, timeout=120), "order": order(env),
                           "stream": stream.read_text() if stream.is_file() else None}
        # ... and one whose stand-in CLI removed its (empty) workdir before it exits 3: an exec in that workdir now
        # fails (ShellEnv refuses it as Docker does: exit 126)
        env = ShellEnv(scratch / "gone-c")
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text('#!/bin/bash\nrmdir -- "$PWD" && exit 3\n'
                                                                        'exit 4\n')
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        plan = write_plan(scratch / "gone", "codex", deploy=False, path_prepend=["/installed-agent/fakebin"])
        report["workdir_gone"] = {"raised": run(make("codex", plan), env, timeout=120),
                                  "workdir": (env.root / "app").exists()}
        # A stream's later step whose exec fails as runc's does, in the container where the step before it ran for real
        # (a stand-in CLI that prints a line and exits 0) and left its stream there
        env = RuncFailedEnv(scratch / "step-c")
        env.runs = 1
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text("#!/bin/bash\necho 'the first step'\n")
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        agent = make("codex", write_plan(scratch / "step-one", "codex", deploy=False,
                                         path_prepend=["/installed-agent/fakebin"]))
        first = run(agent, env, timeout=120)
        stream = env.root / "logs" / "agent" / "agent-codex.jsonl"
        left = stream.read_text() if stream.is_file() else None
        agent.set_plan(str(write_plan(scratch / "step-two", "codex", deploy=False,
                                      path_prepend=["/installed-agent/fakebin"])))
        report["later_step"] = {"first": first, "left": left, "raised": run(agent, env, timeout=120),
                                "stream": stream.exists()}
        # The session's exec, or the look for its stream after a non-zero exit, whose compose client this machine could
        # not spawn (an OSError from the exec itself: EMFILE, EAGAIN on a loaded host); the exec by name as well
        for name, raising, code, fields in (
                ("unspawned", {"bash /installed-agent/hermit/run/launch.sh": errno.EMFILE}, 0, {}),
                ("unspawned_by_name", {"bash /installed-agent/hermit/run/launch.sh": errno.EAGAIN}, 0,
                 {"env_from_host": {"FIREWORKS_API_KEY": "FIREWORKS_API_KEY"}}),
                ("probe_unspawned", {"test -e /logs/agent/agent-codex.jsonl": errno.EAGAIN}, 3, {})):
            env = FakeEnv(raising=raising, launch_code=code)
            plan = write_plan(scratch / name, "codex", deploy=False, **fields)
            report[name] = {"raised": run(make("codex", plan), env), "order": order(env)}

    elif check == "after_cancelled":
        # Harbor's timeout lands while the copy-back runs (the session has just ended by itself), whose exec answers
        # when run again; and, in another trial, while the refreshed sign-in is returned (its check of the file)
        returned = scratch / "return" / "auth.json"
        plan = write_plan(scratch, "codex", return_files=[{"from": "/installed-agent/hermit-home/auth.json",
                                                           "to": str(returned)}])
        env = FakeEnv(hanging_once=("hermit-copy",), files={"/installed-agent/hermit-home/auth.json": "{}"})
        agent = make("codex", plan)
        report["raised"] = run(agent, env, timeout=1.5)
        report["order"] = order(env)
        report["returned"] = returned.read_text() if returned.is_file() else None
        report["metadata"] = metadata(agent)["metadata"]
        returned = scratch / "in_return" / "return" / "auth.json"
        plan = write_plan(scratch / "in_return", "codex", return_files=[
            {"from": "/installed-agent/hermit-home/auth.json", "to": str(returned)}])
        env = FakeEnv(hanging_once=("test -f /installed-agent/hermit-home/auth.json",),
                      files={"/installed-agent/hermit-home/auth.json": "{}"})
        agent = make("codex", plan)
        report["in_return"] = {"raised": run(agent, env, timeout=1.5), "order": order(env),
                               "returned": returned.read_text() if returned.is_file() else None,
                               "metadata": metadata(agent)["metadata"]}

    elif check == "after_bounded":
        # Harbor's timeout cancels the session and the container hangs after it (the copy-back and the collect do not
        # answer for 5 s): each step left has AFTER_CANCEL_S, cut to 0.3 s here. Then a second cancellation (the job's,
        # after Harbor's timeout) while the copy-back hangs.
        returned = scratch / "return" / "auth.json"
        plan = write_plan(scratch, "codex", return_files=[{"from": "/installed-agent/hermit-home/auth.json",
                                                           "to": str(returned)}])
        files = {"/installed-agent/hermit-home/auth.json": "{}"}
        bound, executor.AFTER_CANCEL_S = executor.AFTER_CANCEL_S, 0.3
        try:
            env = FakeEnv(hang=True, hanging=("hermit-copy", "hermit-collect"), hang_s=5, files=files)
            agent = make("codex", plan)
            started = time.monotonic()
            raised = run(agent, env, timeout=0.5)
            report["bounded"] = {"raised": raised, "seconds": round(time.monotonic() - started, 1),
                                 "order": order(env), "returned": returned.read_text() if returned.is_file() else None,
                                 "metadata": metadata(agent)["metadata"]}
        finally:
            executor.AFTER_CANCEL_S = bound
        env = FakeEnv(hang=True, hanging=("hermit-copy",), files=files)
        agent = make("codex", plan)

        async def cancelled_twice():
            task = asyncio.ensure_future(agent.run(INSTRUCTION, env, AgentContext()))
            deadline = time.monotonic() + 30
            while not env.running and time.monotonic() < deadline:             # the session runs
                await asyncio.sleep(0.02)
            task.cancel()                                                      # Harbor's timeout
            while not any("hermit-copy" in c[2] for c in calls(env, "exec")) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)                                      # the copy-back hangs
            task.cancel()                                                      # the job's cancellation
            started = time.monotonic()
            try:
                await asyncio.wait_for(task, 30)
                raised = None
            except BaseException as exc:                   # noqa: BLE001 - reported to the test
                raised = [type(exc).__name__, str(exc)]
            return raised, round(time.monotonic() - started, 1)
        raised, seconds = asyncio.run(cancelled_twice())
        report["twice"] = {"raised": raised, "seconds": seconds, "order": order(env),
                           "metadata": metadata(agent)["metadata"]}

    elif check == "return_check":
        # The check of a file to return in a container whose compose service had stopped (its exec fails, saying so),
        # and in one where the session left no such file
        stopped = {"test -f /installed-agent/hermit-home/auth.json": 'service "main" is not running\n'}
        for name, env in (("failed", FakeEnv(failing=stopped, files={"/installed-agent/hermit-home/auth.json": "{}"})),
                          ("absent", FakeEnv())):
            returned = scratch / name / "return" / "auth.json"
            agent = make("codex", write_plan(scratch / name, "codex", deploy=False, return_files=[
                {"from": "/installed-agent/hermit-home/auth.json", "to": str(returned)}]))
            report[name] = {"raised": run(agent, env), "downloads": calls(env, "download_file"),
                            "returned": returned.is_file(), "metadata": metadata(agent)["metadata"]}

    elif check == "workdir_removed":
        # A session that writes a refreshed sign-in and its last message, then removes its workdir with the deployment
        # in it, whose copy_back (the adapter's memory) goes with it: the deployment's files, then its folders and the
        # workdir with rmdir, which refuses one holding anything else. For real with bash: it ends by itself, or runs
        # on (a child of its own) until Harbor's timeout cancels the run. Docker refuses every exec in that workdir
        # from then on, and so does ShellEnv (exit 126, runc's words).
        remover = ('#!/bin/bash\necho \'{"refreshed": true}\' > "$CODEX_HOME/auth.json"\n'
                   'echo "the answer" > "$CODEX_HOME/last_message.txt"\n'
                   'rm -f -- AGENTS.md .hermit-deploy.json .codex/hooks.json .codex/config.toml hermit/demo/ADAPTER.md '
                   'hermit/demo/memory/NOTES.md && rmdir -- .codex hermit/demo/memory hermit/demo hermit "$PWD" || '
                   'exit 9\n'
                   'if [ -n "$STUB_SLEEP" ]; then sleep "$STUB_SLEEP" & echo "$!" > "$HERMIT_HOOK_LOGS/child.pid"; '
                   "wait; fi\n")

        def alive(pid: int) -> bool:                       # the session's child, not a zombie
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                return False
            return b"sleep" in cmdline and not re.search(r"^State:\s+Z", status, re.M)
        for name, sleep in (("ended", None), ("running", "120")):
            env = ShellEnv(scratch / f"{name}-c")
            (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
            (env.root / "installed-agent" / "fakebin" / "codex").write_text(remover)
            (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
            returned = scratch / name / "returned" / "auth.json"
            plan = write_plan(scratch / name, "codex", path_prepend=["/installed-agent/fakebin"],
                              env={"CODEX_HOME": "/installed-agent/hermit-home", **({"STUB_SLEEP": sleep} if sleep
                                                                                     else {})},
                              return_files=[{"from": "/installed-agent/hermit-home/auth.json", "to": str(returned)}])
            agent, logs = make("codex", plan), env.root / "logs" / "agent"
            raised = run(agent, env, cancel_when=(logs / "child.pid").is_file) if sleep else \
                run(agent, env, timeout=120)
            child, deadline = (logs / "child.pid"), time.monotonic() + 10
            while child.is_file() and alive(int(child.read_text())) and time.monotonic() < deadline:
                time.sleep(0.1)
            report[name] = {"raised": raised, "workdir": (env.root / "app").exists(),
                            "returned": returned.read_text() if returned.is_file() else None,
                            "copied": sorted(p.name for p in (logs / "hermit-copy").glob("*"))
                            if (logs / "hermit-copy").is_dir() else None,
                            "collected": sorted(p.name for p in (logs / "hermit-collect").glob("*")),
                            "child_alive": child.is_file() and alive(int(child.read_text())),
                            "metadata": metadata(agent)["metadata"]}

    elif check == "copy_fails":
        # For real with bash, after a session of the stand-in CLI that ended by itself: a cp first on the container's
        # PATH that fails part-way (FAILING_CP) in the copy-back of the adapter's memory and in the collect of
        # last_message.txt and of a sessions folder, and copies the part after them in each (a folder of the
        # adapter's own, the home's agents folder) as cp does: a file of mode 600 in each (the home is private), which
        # only the step's chmod after the failed part makes readable in the copy
        failing_bin = scratch / "failing-cp-bin"
        failing_bin.mkdir()
        (failing_bin / "cp").write_text(FAILING_CP)
        (failing_bin / "cp").chmod(0o755)
        env = ShellEnv(scratch / "c", path=f"{failing_bin}:{os.environ['PATH']}")
        (env.root / "installed-agent" / "fakebin").mkdir(parents=True)
        (env.root / "installed-agent" / "fakebin" / "codex").write_text(STUB_CLI)
        (env.root / "installed-agent" / "fakebin" / "codex").chmod(0o755)
        journal = make_deploy(scratch, "codex") / "hermit" / "demo" / "journal"
        journal.mkdir()
        (journal / "JOURNAL.md").write_text("# Journal\n")
        (journal / "JOURNAL.md").chmod(0o600)
        sessions = make_home(scratch) / "sessions"
        sessions.mkdir()
        (sessions / "rollout-1.jsonl").write_text('{"type": "session_meta"}\n')
        home = "/installed-agent/hermit-home"
        plan = write_plan(scratch, "codex", path_prepend=["/installed-agent/fakebin"], collect=[
            {"from": f"{home}/last_message.txt", "to": "last_message.txt"},
            {"from": f"{home}/sessions", "to": "sessions"}, {"from": f"{home}/agents", "to": "agents"}])
        data = json.loads(plan.read_text())
        data["deploy"]["copy_back"] = ["hermit/demo/memory", "hermit/demo/journal"]
        plan.write_text(json.dumps(data))
        agent = make("codex", plan)
        report["raised"] = run(agent, env, timeout=120)
        logs = env.root / "logs" / "agent"
        report["entries"] = sorted(p.relative_to(logs).as_posix() for d in ("hermit-copy", "hermit-collect")
                                   for p in (logs / d).rglob("*"))
        report["kept"] = {p.relative_to(logs).as_posix(): [p.read_text(), oct(p.stat().st_mode & 0o777)]
                          for d in ("hermit-copy", "hermit-collect") for p in sorted((logs / d).rglob("*"))
                          if p.is_file()}
        sources = {"hermit-copy/journal/JOURNAL.md": "app/hermit/demo/journal/JOURNAL.md",       # in the container
                   "hermit-collect/agents/reviewer.toml": "installed-agent/hermit-home/agents/reviewer.toml"}
        report["source_modes"] = {rel: oct((env.root / src).stat().st_mode & 0o777) for rel, src in sources.items()}
        report["metadata"] = metadata(agent)["metadata"]

    elif check in ("shell_session", "shell_cancel", "shell_token"):
        env = ShellEnv(scratch / "c")
        report["root"] = str(env.root)
        fakebin = env.root / "installed-agent" / "fakebin"
        fakebin.mkdir(parents=True)
        cli = "claude" if check == "shell_token" else "codex"
        (fakebin / PROGRAM[cli]).write_text(STUB_CLI)
        (fakebin / PROGRAM[cli]).chmod(0o755)
        fields = {"path_prepend": ["/installed-agent/fakebin"],
                  "return_files": [{"from": "/installed-agent/hermit-home/auth.json",
                                    "to": str(scratch / "returned" / "auth.json")}],
                  "collect": [{"from": "/installed-agent/hermit-home/last_message.txt", "to": "last_message.txt"},
                              {"from": "/installed-agent/hermit-home/auth.json", "to": "auth.json"}]}
        if check == "shell_session":
            fields["env_from_host"] = {"FIREWORKS_API_KEY": "FIREWORKS_API_KEY", "OPENAI_API_KEY": "FIREWORKS_API_KEY"}
        if check == "shell_cancel":
            fields["env"] = {"CODEX_HOME": "/installed-agent/hermit-home", "STUB_SLEEP": "120"}
        if check == "shell_token":
            fields.update(stdin=True, command=["claude", "-p", "--output-format", "stream-json"],
                          env_by_fd={"CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"})
        plan = write_plan(scratch, cli, deploy=check != "shell_cancel", **fields)
        agent = make(cli, plan)
        logs = env.root / "logs" / "agent"
        started = time.monotonic()
        if check == "shell_cancel":                        # once the session has started something of its own
            report["raised"] = run(agent, env, cancel_when=(logs / "child.pid").is_file)
        else:
            report["raised"] = run(agent, env, timeout=120)
        report["seconds"] = round(time.monotonic() - started, 1)

        def listing(d: Path) -> list:
            return sorted(str(p.relative_to(d)) for p in d.rglob("*")) if d.is_dir() else []

        def text(p: Path):
            return p.read_text() if p.is_file() else None
        report["stream"] = text(logs / f"agent-{cli}.jsonl")
        report["copied"] = listing(logs / "hermit-copy")
        report["copied_notes"] = text(logs / "hermit-copy" / "memory" / "NOTES.md")
        report["collected"] = listing(logs / "hermit-collect")
        report["returned"] = text(scratch / "returned" / "auth.json")
        report["pidfile_left"] = (env.root / "installed-agent" / "hermit" / "run" / "session.pid").exists()
        if (logs / "child.pid").is_file():               # what the session started in the background

            def alive(pid: int) -> bool:
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                except OSError:
                    return False
                return b"sleep" in cmdline and not re.search(r"^State:\s+Z", status, re.M)
            pid, deadline = int((logs / "child.pid").read_text()), time.monotonic() + 10
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            report["child_alive"] = alive(pid)
        home = env.root / "installed-agent" / "hermit-home"
        report["home_modes"] = {p.name: oct(p.stat().st_mode & 0o777) for p in home.iterdir() if p.is_file()}
        report["home_config"] = (home / "config.toml").read_text()
        report["leaks"] = leaks(env)
        report["token_sha"] = hashlib.sha256((FAKE_TOKEN + "\n").encode()).hexdigest()[:16]
        report["metadata"] = metadata(agent)["metadata"]

    elif check == "secrets_refused":
        plan = write_plan(scratch, "codex", deploy=False, env_from_host={"FIREWORKS_API_KEY": "FIREWORKS_API_KEY"})
        report["plain_env"] = run(make("codex", plan), PlainEnv())
        os.environ.pop("FIREWORKS_API_KEY")
        env = FakeEnv()
        report["missing"] = run(make("codex", plan), env)
        report["missing_compose"] = calls(env, "compose")

    elif check == "installs":
        def install(cli, env, **kwargs):
            agent = make(cli, **kwargs)
            raised = None
            try:
                asyncio.run(agent.setup(env))
            except BaseException as exc:                   # noqa: BLE001 - reported to the test
                raised = [type(exc).__name__, str(exc)]
            return {"raised": raised, "mounted": agent._mounted,
                    "execs": [(c[1], c[2]) for c in env.calls if c[0] == "exec"],
                    "uploads": [c[1] for c in env.calls if c[0] == "upload_file"]}

        codex_bin = {"codex_bin": "/opt/hermit-codex/bin/codex", "version": "0.164.0"}
        report["codex_mounted"] = install("codex", FakeEnv(mounted={codex_bin["codex_bin"]: "codex-cli 0.164.0\n"}),
                                          **codex_bin)
        bare = FakeEnv(mounted={codex_bin["codex_bin"]: "codex-cli 0.164.0\n"})
        answer = bare._answer

        async def no_ca_bundle(c, user):                   # TB2.0's bare ubuntu:24.04 has none at all
            if c.startswith("[ -s /etc/ssl/certs/ca-certificates.crt ]"):
                return ExecResult(stdout="", stderr="", return_code=1)
            return await answer(c, user)
        bare._answer = no_ca_bundle
        report["codex_bare"] = install("codex", bare, **codex_bin)
        report["codex_other"] = install("codex", FakeEnv(mounted={codex_bin["codex_bin"]: "codex-cli 0.160.0\n"}),
                                        **codex_bin)
        report["claude_mounted"] = install("claude", FakeEnv(mounted={"/opt/hermit-claude/claude":
                                                                      "2.1.318 (Claude Code)\n"}),
                                           version="2.1.318", claude_bin="/opt/hermit-claude/claude")
        report["claude"] = install("claude", FakeEnv(), version="2.1.318")
        report["pi"] = install("pi", FakeEnv(), version="1.5.0")
        report["pi_mounted"] = install("pi", FakeEnv(mounted={"/opt/hermit-pi/pi": "1.5.0\n"}), version="1.5.0",
                                       pi_bin="/opt/hermit-pi/pi")
        # ... among other lines before and after its own (Codex's parse takes the first line, Pi's the last)
        report["codex_mounted_noisy"] = install("codex", FakeEnv(noise=True, mounted={
            codex_bin["codex_bin"]: "codex-cli 0.164.0\n"}), **codex_bin)
        report["pi_mounted_noisy"] = install("pi", FakeEnv(noise=True, mounted={"/opt/hermit-pi/pi": "1.5.0\n"}),
                                             version="1.5.0", pi_bin="/opt/hermit-pi/pi")
        report["opencode"] = install("opencode", FakeEnv(), version="2.2.6")
        report["opencode_bare"] = install("opencode", FakeEnv(missing={"curl", "wget", "python3"}), version="2.2.6")
        report["opencode_mounted"] = install("opencode", FakeEnv(mounted={"/opt/hermit-opencode/opencode": "2.2.6\n"}),
                                             version="2.2.6", opencode_bin="/opt/hermit-opencode/opencode")
        report["dsh"] = install("dsh", FakeEnv(), version="0.2.2-rc.1")
        report["dsh_mounted"] = install("dsh", FakeEnv(mounted={"/opt/hermit-dsh/dsh": "dsh 0.2.2-rc.1\n"}),
                                        version="0.2.2-rc.1", dsh_bin="/opt/hermit-dsh/dsh")
        report["mini"] = install("mini-swe-agent", FakeEnv(), version="2.4.6")
        report["setup_timeouts"] = {cli: agent_class(cli).DEFAULT_SETUP_TIMEOUT_SEC for cli in CLASSES}
        # The image tests of the OpenCode and dsh scripts, run with bash under the pipefail Harbor's exec_as_root and
        # exec_as_agent put first, a stand-in ldd as each libc's answers (musl's on stderr, then exit 1) and a curl
        # that fails.
        fakebin = scratch / "fakebin"
        fakebin.mkdir()
        (fakebin / "curl").write_text(f"#!/bin/sh\necho \"curl $*\" >> {scratch}/curl.log\nexit 1\n")
        (fakebin / "curl").chmod(0o755)
        opencode = make("opencode", version="2.2.6").install_command()
        url_only = opencode[:opencode.index('rm -rf "$d"')] + 'echo "$url"\n'       # no download: the URL picked
        dsh = make("dsh", version="0.2.2-rc.1").install_command()
        report["musl_test"] = {}
        for libc, ldd in (("musl", "echo 'musl libc (x86_64)\nVersion 1.2.4' >&2; exit 1"),
                          ("glibc", "echo 'ldd (GNU libc) 2.36'")):
            (fakebin / "ldd").write_text(f"#!/bin/sh\n{ldd}\n")
            (fakebin / "ldd").chmod(0o755)
            env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(scratch / f"home-{libc}")}
            got = subprocess.run(["bash", "-c", f"set -o pipefail; if {executor.MUSL}; then echo musl; else echo "
                                  "glibc; fi"], capture_output=True, text=True, env=env, timeout=60)
            report["musl_test"][libc] = got.stdout.strip()
            got = subprocess.run(["bash", "-c", "set -o pipefail; " + url_only], capture_output=True, text=True,
                                 env=env, timeout=60)
            report[f"opencode_url_{libc}"] = got.stdout.strip()
            (scratch / "curl.log").unlink(missing_ok=True)
            got = subprocess.run(["bash", "-c", "set -o pipefail; " + dsh], capture_output=True, text=True, env=env,
                                 timeout=60)
            curl_log = scratch / "curl.log"
            report[f"dsh_{libc}"] = {"code": got.returncode, "stderr": got.stderr[-400:],
                                     "curl": curl_log.read_text() if curl_log.exists() else ""}
        # A mount whose binary did not serve is left off PATH, so that the installed CLI is the one found.
        plan = write_plan(scratch / "mount", "codex", deploy=False, path_prepend=["/opt/hermit-codex/bin", "/x/bin"])
        agent = make("codex", plan, **codex_bin)
        session = {"argv": ["codex"], "stream": "agent-codex.jsonl"}
        report["path_unmounted"] = [line for line in agent.launch_script(agent._plan, session, "/app").splitlines()
                                    if line.startswith("export PATH")]
        agent._mounted = True
        report["path_mounted"] = [line for line in agent.launch_script(agent._plan, session, "/app").splitlines()
                                  if line.startswith("export PATH")]

    elif check == "install_failures":
        # The setup Harbor runs before the trial's agent phase: dsh refusing a musl image, Pi's npm install failing,
        # and Pi's npm install hanging until Harbor's setup timeout cancels it
        musl = "hermit: dsh needs Node 24.9 or later from nvm, whose builds need glibc, and this image is musl\n"
        npm = ("npm error code ETARGET\nnpm error notarget No matching version found for "
               "@earendil-works/pi-coding-agent@1.5.0.\n")
        for name, cli, version, env, timeout in (
                ("dsh_musl", "dsh", "0.2.2-rc.1", FakeEnv(failing={"@deepseek-ai/dsh": musl}), None),
                ("pi_npm", "pi", "1.5.0", FakeEnv(failing={"pi-coding-agent": npm}), None),
                ("pi_setup_timeout", "pi", "1.5.0", FakeEnv(hanging=("pi-coding-agent",)), 0.5)):
            work = make(cli, version=version).setup(env)
            try:
                asyncio.run(work if timeout is None else asyncio.wait_for(work, timeout))
                report[name] = None
            except BaseException as exc:                   # noqa: BLE001 - reported to the test
                report[name] = [type(exc).__name__, str(exc)]

    elif check == "mount_rejected":
        # The plan as hermit.harbor.launch renders it for a mounted codex_bin: the mount's path is the command's
        # program and its folder first on PATH. The mount answers another version here, or is the asked-for one; or is
        # the asked-for one that only root may run (mode 700, a uid of this machine's) in a task whose agent user is
        # not root, which Harbor makes the default user for the install as for the session (with_default_user); or is
        # the asked-for one, which that user may run, under a HOME that user cannot write (Codex warns first there)
        codex_bin = "/opt/hermit-codex/bin/codex"
        for name, said, user in (("rejected", "codex-cli 0.160.0\n", None), ("served", "codex-cli 0.164.0\n", None),
                                 ("root_only", "codex-cli 0.164.0\n", "agent"),
                                 ("home_unwritable", "codex-cli 0.164.0\n", "agent")):
            plan = write_plan(scratch / name, "codex", deploy=False, nvm=True, path_prepend=["/opt/hermit-codex/bin"],
                              command=[codex_bin, "exec", "-C", "@@HERMIT_WORKDIR@@", "@@HERMIT_PROMPT@@"])
            env = FakeEnv(mounted={codex_bin: said}, root_only=[codex_bin] if name == "root_only" else (),
                          warns=[codex_bin] if name == "home_unwritable" else ())
            env.default_user = user
            agent = make("codex", plan, codex_bin=codex_bin, version="0.164.0")
            asyncio.run(agent.setup(env))
            installed = any("npm install -g @openai/codex@0.164.0" in c[2] for c in calls(env, "exec"))
            # what the mount was asked, and as whom (None: Harbor's default user)
            asked = [[c[1], "version" if "--version" in c[2] else "test -x"] for c in calls(env, "exec")
                     if f"{codex_bin} --version" in c[2] or c[2] == f"test -x {codex_bin}"]
            report[name] = {"raised": run(agent, env), "mounted": agent._mounted, "installed": installed,
                            "asked": asked, "launch": uploaded(env).get("/installed-agent/hermit/run/launch.sh")}

    elif check == "codex_nvm":
        # Harbor's Codex install on a glibc image: codex under the agent user's nvm, none on the image's PATH (its
        # link into /usr/local/bin, made without nvm, finds none). For real with bash: that user's HOME, whose nvm.sh
        # puts its node's bin (a stand-in codex there) on PATH, a container PATH without codex, a plan that loads nvm
        # (as hermit.harbor.launch renders Codex's); and then a mounted codex as well (the plan's program, its folder
        # on the plan's PATH, as launch renders a mounted codex_bin's), the mount having served.
        stand_in = '#!/bin/bash\necho "ran: $0"\necho "codex on PATH: $(command -v codex)"\n'
        mount = "/opt/hermit-codex/bin/codex"
        for name in ("installed", "mounted"):
            env = ShellEnv(scratch / f"{name}-c", path="/usr/bin:/bin", home=scratch / f"{name}-c" / "home" / "agent")
            (env.home / ".nvm").mkdir(parents=True)
            (env.home / ".nvm" / "nvm.sh").write_text('export PATH="$NVM_DIR/versions/node/v22.12.0/bin:$PATH"\n')
            programs = [env.home / ".nvm" / "versions" / "node" / "v22.12.0" / "bin" / "codex"]
            fields, kwargs = {}, {}
            if name == "mounted":
                programs.append(Path(env.map(mount)))
                fields = {"command": [mount, "exec", "-C", "@@HERMIT_WORKDIR@@", "@@HERMIT_PROMPT@@"],
                          "path_prepend": ["/opt/hermit-codex/bin"]}
                kwargs = {"codex_bin": mount, "version": "0.164.0"}
            for program in programs:
                program.parent.mkdir(parents=True, exist_ok=True)
                program.write_text(stand_in)
                program.chmod(0o755)
            agent = make("codex", write_plan(scratch / name, "codex", deploy=False, nvm=True, **fields), **kwargs)
            agent._mounted = name == "mounted"             # as the install leaves it when the mount served
            stream = env.root / "logs" / "agent" / "agent-codex.jsonl"
            report[name] = {"raised": run(agent, env, timeout=120), "programs": [str(p) for p in programs],
                            "stream": stream.read_text() if stream.is_file() else None}

    elif check == "mini_swe_agent":
        bundle = make_bundle(scratch / "bundle")
        settings = {"model": "accounts/fireworks/models/kimi", "effort": "medium",
                    "binding": {"litellm": "fireworks_ai/accounts/fireworks/models/kimi",
                                "base_url": "https://api.fireworks.ai/inference/v1",
                                "api_key_env": "FIREWORKS_API_KEY"}}
        for name, imports, noise in (("bundle", True, False), ("no_import", False, False), ("noisy", True, True)):
            base = scratch / name
            plan = write_plan(base, "mini-swe-agent", stdin=True,
                              command=["python3", "-P", "-m",              # hermit.harbor.launch's `bin` for it
                                       "hermit.agent.base.mini_swe_agent_run", "--settings",
                                       "/installed-agent/hermit-home/mini-swe-agent.json", "--cwd",
                                       "@@HERMIT_WORKDIR@@", "--last", "/installed-agent/hermit-home/last_message.txt",
                                       "--stdin"],
                              bundle={"host_dir": str(bundle), "container_dir": "/installed-agent/bundle"},
                              env={"MSWEA_GLOBAL_CONFIG_DIR": "/installed-agent/hermit-home",
                                   "PYTHONPATH": "/installed-agent/bundle:@@HERMIT_WORKDIR@@/lib",
                                   "HERMIT_TOOLS_URL": "http://10.100.0.1:5000/tok"},
                              env_from_host={"FIREWORKS_API_KEY": "FIREWORKS_API_KEY"},
                              tools_script={"url_env": "HERMIT_TOOLS_URL"})
            (base / "launch" / "home" / "mini-swe-agent.json").write_text(json.dumps(settings))
            env = FakeEnv(bundle_ok=imports, noise=noise)
            agent = make("mini-swe-agent", plan)
            raised = run(agent, env)
            files = uploaded(env)
            report[name] = {"raised": raised, "launch": files.get("/installed-agent/hermit/run/launch.sh"),
                            "tool": files.get("/installed-agent/hermit/bin/hermit-tool"),
                            "bundle_upload": [c[1] for c in calls(env, "upload_dir")],
                            "probe": [(c[2], c[3]) for c in calls(env, "exec") if "import hermit" in c[2]],
                            "leaks": leaks(env)}

    elif check == "tool_script":
        seen = []
        # What GET /<name>/tools is answered with: an HTTP error, as a JSON object saying so (its "error", FastAPI's
        # "detail"), a JSON string, a number; never the tools' list
        not_tools = {"gone": ({"error": "no such path"}, 404), "detail": ({"detail": "Not Found"}, 404),
                     "text": ("Bad Gateway", 502), "number": (5, 500)}
        # ... or with a list that holds no tools: items that are not objects, an object without a name
        not_tool_items = {"strings": ["lookup"], "nameless": [{"description": "Finds a row."}]}

        class Tools(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                name = self.path.split("/")[1]
                if self.path == "/tok/tools":
                    self._send([{"name": "lookup", "description": "Finds a row."}])
                elif name in not_tool_items:               # /<name>/tools: not_tool_items[name], with a 200
                    self._send(not_tool_items[name])
                else:                                      # /<name>/tools: not_tools[name]
                    self._send(*not_tools[name])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                self._send({"result": {"text": f"row {body.get('id')}"}})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Tools)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        script = scratch / "hermit-tool"
        script.write_text(executor.TOOL_SCRIPT.replace("__PYTHON__", sys.executable)
                          .replace("__URL_ENV__", "HERMIT_TOOLS_URL"))
        script.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "HERMIT_TOOLS_URL": f"http://127.0.0.1:{server.server_address[1]}/tok"}
        for name, args in (("list", []), ("call", ["lookup", '{"id": 7}']), ("unknown", ["nope", "{}"]),
                           ("bad_json", ["lookup", "{oops"]), ("too_many", ["lookup", "{}", "x"])):
            got = subprocess.run([str(script), *args], capture_output=True, text=True, env=env, timeout=60)
            report[name] = [got.returncode, got.stdout.strip()]
        got = subprocess.run([str(script)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
        report["no_url"] = [got.returncode, got.stdout.strip()]
        # URLs whose GET /tools the server answers with an HTTP error, each in a shape of not_tools
        report["list_error"] = {}
        for name in not_tools:
            got = subprocess.run([str(script)], capture_output=True, text=True, timeout=60, env={
                **env, "HERMIT_TOOLS_URL": f"http://127.0.0.1:{server.server_address[1]}/{name}"})
            report["list_error"][name] = [got.returncode, got.stdout.strip(), got.stderr.strip()[-300:]]
        report["list_not_tools"] = {}
        for name in not_tool_items:
            got = subprocess.run([str(script)], capture_output=True, text=True, timeout=60, env={
                **env, "HERMIT_TOOLS_URL": f"http://127.0.0.1:{server.server_address[1]}/{name}"})
            report["list_not_tools"][name] = [got.returncode, got.stdout.strip(), got.stderr.strip()[-300:]]
        report["seen"] = seen
        server.shutdown()
        server.server_close()

    elif check == "answer":
        import logging
        from harbor.agents.capabilities import AgentCapabilities
        from harborside import answer as answer_module
        HermitAnswer = answer_module.HermitAnswer
        answer_module.POLL_S = 0.05
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            os.environ[name] = "http://127.0.0.1:9"        # a proxy that is not there: an endpoint here is reached
                                                           # directly
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        requests, replies = [], []

        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                status, payload = replies.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/endpoint-token-1f2e3d"

        def answer_agent(name, **spec):
            base = scratch / name
            base.mkdir(parents=True, exist_ok=True)
            plan = {"version": 1, "cli": "answer", "label": "answer", "metadata": {"plan_digest": "d1"},
                    "answer": {"endpoint": endpoint, "model": "kimi", "system": "Context: tables are in /app/data.",
                               "instruction_file": None, "wait_s": 5, "attachments": [], "answer_file": None,
                               "answer_pattern": None, "answer_format": "{answer}", "max_tokens": None,
                               "effort": None, **spec}}
            (base / "plan.json").write_text(json.dumps(plan))
            return HermitAnswer(logs_dir=base / "logs", plan=str(base / "plan.json"))

        def answer_run(agent, env, instruction="What is 6 x 7?"):
            context = AgentContext()
            try:
                asyncio.run(agent.run(instruction, env, context))
            except BaseException as exc:                   # noqa: BLE001 - reported to the test
                return [type(exc).__name__, str(exc)], context.model_dump()
            return None, context.model_dump()

        png = b"\x89PNG\r\n\x1a\nfake"
        env = FakeEnv(files={"/app/instruction.md": "Which panel shows the knockdown?\n", "/app/fig.png": png,
                             "/app/notes.txt": b"not an image"},
                      misses=2, listing="/app/fig.png\n/app/notes.txt\n/app/fig.png\n")
        replies.append((200, {"model": "kimi-served", "choices": [{"message": {"role": "assistant", "content":
                        "First [ANSWER]A[/ANSWER], then on reflection [ANSWER] C [/ANSWER]."}}],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 9,
                                  "prompt_tokens_details": {"cached_tokens": 100}}}))
        full = answer_agent("full", instruction_file="/app/instruction.md", attachments=["/app/*.png", "/app/*.txt"],
                            answer_file="/app/answer.txt", answer_pattern=r"\[ANSWER\](.*?)\[/ANSWER\]",
                            answer_format="[ANSWER]{answer}[/ANSWER]", max_tokens=256, effort="high")
        report["raised"], report["context"] = answer_run(full, env, "Read /app/instruction.md and answer.")
        report["request"] = requests[-1]
        writes = [c[2] for c in calls(env, "exec") if "base64 -d" in c[2]]
        report["writes"] = writes
        report["written"] = base64.b64decode(re.search(r"printf '%s' (\S+) \|", writes[0]).group(1).strip("'")
                                             ).decode() if writes else None
        report["write_users"] = [c[1] for c in calls(env, "exec") if "base64 -d" in c[2]]
        report["answer_json"] = json.loads((scratch / "full" / "logs" / "answer.json").read_text())
        report["polls"] = sum(1 for c in calls(env, "exec") if c[2].startswith("test -f /app/instruction.md"))
        report["png_url"] = "data:image/png;base64," + base64.b64encode(png).decode()
        # No match of the pattern: the whole reply; no answer file: nothing written; Harbor's instruction asked.
        replies.append((200, {"choices": [{"message": {"content": [{"type": "text", "text": "Just 42."}]}}]}))
        plain, env = answer_agent("plain", answer_pattern=r"\[ANSWER\](.*)"), FakeEnv()
        report["plain_raised"], _ = answer_run(plain, env)
        report["plain_request"] = requests[-1][1]
        report["plain_answer"] = json.loads((scratch / "plain" / "logs" / "answer.json").read_text())
        report["plain_execs"] = [c[2] for c in calls(env, "exec")]
        # The model failed: NonZeroAgentExitCodeError (the verifier still runs), the endpoint's token in no message.
        replies.append((500, {"error": {"message": "upstream exploded"}}))
        report["failed"], _ = answer_run(plain, FakeEnv())
        report["late"], _ = answer_run(answer_agent("late", instruction_file="/app/instruction.md", wait_s=0.2),
                                       FakeEnv())
        # What is read from the container (the question, a figure's size and bytes) comes with other lines before and
        # after it, as Harbor's docker exec merges stderr into stdout: for real with bash
        noisy = ShellEnv(scratch / "noisy-c", noise=True)
        (noisy.root / "app" / "instruction.md").write_text("Which panel shows the knockdown?\n")
        (noisy.root / "app" / "fig.png").write_bytes(png)
        replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))
        report["noisy_raised"], _ = answer_run(answer_agent(
            "noisy", instruction_file="/app/instruction.md", attachments=["/app/*.png"], answer_file="/app/answer.txt",
            answer_pattern=r"\[ANSWER\](.*?)\[/ANSWER\]"), noisy)
        report["noisy_request"] = requests[-1][1] if report["noisy_raised"] is None else None
        answered = noisy.root / "app" / "answer.txt"
        report["noisy_written"] = answered.read_text() if answered.is_file() else None
        # An image without base64 (for real with bash, its PATH without one): the question and a figure
        nobin = scratch / "no-base64-bin"
        nobin.mkdir()
        for tool in ("bash", "tr", "wc"):
            (nobin / tool).symlink_to(shutil.which(tool))
        bare = ShellEnv(scratch / "no-base64-c", path=str(nobin))
        (bare.root / "app" / "instruction.md").write_text("Which panel shows the knockdown?\n")
        (bare.root / "app" / "fig.png").write_bytes(png)
        asked = len(requests)
        replies.extend([(200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]})] * 2)   # if it asks
        report["no_base64_question"], _ = answer_run(answer_agent(
            "no_base64_question", instruction_file="/app/instruction.md"), bare)
        report["no_base64_figure"], _ = answer_run(answer_agent("no_base64_figure", attachments=["/app/*.png"]), bare)
        report["no_base64_asked"] = len(requests) - asked

        def figure_run(name, env):
            """A run whose one figure cannot be listed, measured or read: what it raised, and how often the model was
            asked."""
            asked = len(requests)
            replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))   # if it asks
            raised, _ = answer_run(answer_agent(name, attachments=["/app/*.png"]), env)
            return {"raised": raised, "asked": len(requests) - asked}

        # A figure that cannot be measured or read is never left out: for real with bash (in the C locale, as the test
        # matches what bash says), an image without wc (its PATH without one) and a figure of mode 000; on FakeEnv's
        # stand-ins, a read whose exec failed otherwise (compose's service stopped), a measure that exits 0 without its
        # line or with one whose value is no number, and a read that exits 0 without its line
        nowc = scratch / "no-wc-bin"
        nowc.mkdir()
        for tool in ("bash", "base64", "tr"):
            (nowc / tool).symlink_to(shutil.which(tool))
        unmeasured = ShellEnv(scratch / "no-wc-c", path=str(nowc), locale="C")
        (unmeasured.root / "app" / "fig.png").write_bytes(png)
        report["no_wc_figure"] = figure_run("no_wc_figure", unmeasured)
        locked = ShellEnv(scratch / "locked-c", locale="C")
        (locked.root / "app" / "fig.png").write_bytes(png)
        (locked.root / "app" / "fig.png").chmod(0)
        report["locked_figure"] = figure_run("locked_figure", locked)
        (locked.root / "app" / "fig.png").chmod(0o644)
        stopped = FakeEnv(files={"/app/fig.png": png}, listing="/app/fig.png\n",
                          failing={"hermit-data:": 'service "main" is not running\n'})   # docker compose exec
        report["failed_read_figure"] = figure_run("failed_read_figure", stopped)
        for name, part, said in (("unmarked_size_figure", "hermit-size:", "something else\n"),
                                 ("odd_size_figure", "hermit-size:", "hermit-size:12 abc\n"),
                                 ("unmarked_data_figure", "hermit-data:", "something else\n")):
            report[name] = figure_run(name, FakeEnv(files={"/app/fig.png": png}, listing="/app/fig.png\n",
                                                    answers={part: (0, said)}))
        # Nor are the figures left out when the exec that lists the globs failed (compose's service stopped)
        unlisted = FakeEnv(files={"/app/fig.png": png}, listing="/app/fig.png\n",
                           failing={"shopt -s nullglob": 'service "main" is not running\n'})
        report["failed_listing"] = figure_run("failed_listing", unlisted)
        # One over MAX_ATTACHMENT_BYTES (a sparse file a byte over) is left out with its warning, and the model is
        # asked with the others: for real with bash
        sized = ShellEnv(scratch / "over-limit-c")
        (sized.root / "app" / "fig.png").write_bytes(png)
        with open(sized.root / "app" / "huge.png", "wb") as huge:
            huge.truncate(answer_module.MAX_ATTACHMENT_BYTES + 1)
        over, warned, catch = answer_agent("over_limit", attachments=["/app/*.png"]), [], logging.Handler()
        catch.emit = lambda record: warned.append(record.getMessage().replace(str(sized.root), ""))
        over.logger.addHandler(catch)
        replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))
        report["over_limit_raised"], _ = answer_run(over, sized)
        over.logger.removeHandler(catch)
        report["over_limit_content"] = requests[-1][1]["messages"][-1]["content"]
        report["over_limit_warned"], report["max_bytes"] = warned, answer_module.MAX_ATTACHMENT_BYTES
        # A glob that matches no file is no failure: the model is asked once, with the plain question (for real with
        # bash, an /app that holds no figure)
        unmatched = ShellEnv(scratch / "no-match-c")
        asked = len(requests)
        replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))
        report["no_match_raised"], _ = answer_run(answer_agent("no_match", attachments=["/app/*.png"]), unmatched)
        report["no_match_asked"] = len(requests) - asked
        report["no_match_content"] = requests[-1][1]["messages"][-1]["content"] if report["no_match_asked"] else None
        # The model answered, and this machine could not then run the exec that writes the answer into the task (its
        # compose client not spawned: an OSError from the exec itself, EMFILE, EAGAIN on a loaded host), or could not
        # keep the answer in answer.json (its disk quota reached); and, for contrast, a write the container refused
        refused = "mkdir: cannot create directory '/app': Permission denied\n"
        for name, env in (("write_unspawned", FakeEnv(raising={"base64 -d": errno.EMFILE})),
                          ("write_unspawned_eagain", FakeEnv(raising={"base64 -d": errno.EAGAIN})),
                          ("write_refused", FakeEnv(failing={"base64 -d": refused}))):
            replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))
            raised, _ = answer_run(answer_agent(name, answer_file="/app/answer.txt"), env)
            report[name] = {"raised": raised, "kept": (scratch / name / "logs" / "answer.json").is_file()}

        class QuotaReached(Path):
            """A logs directory on a host whose disk quota is reached: writing a file there fails."""

            def write_text(self, *args, **kwargs):
                raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))
        unkept, env = answer_agent("unkept", answer_file="/app/answer.txt"), FakeEnv()
        unkept.logs_dir = QuotaReached(unkept.logs_dir)
        replies.append((200, {"choices": [{"message": {"content": "[ANSWER]B[/ANSWER]"}}]}))
        raised, _ = answer_run(unkept, env)
        report["unkept"] = {"raised": raised, "logs": str(unkept.logs_dir),
                            "writes": [c[2] for c in calls(env, "exec") if "base64 -d" in c[2]]}
        replies.clear()
        server.shutdown()
        server.server_close()
        report["unreached"], _ = answer_run(plain, FakeEnv())
        report["error_class"] = NonZeroAgentExitCodeError.__name__
        report["capabilities"] = HermitAnswer.capabilities == AgentCapabilities()
        report["name"] = HermitAnswer.name()
        bad = scratch / "bad"
        bad.mkdir()
        (bad / "plan.json").write_text(json.dumps({"version": 1, "cli": "answer", "answer": {
            "endpoint": "ftp://x", "model": "", "attachments": ["/app/a b.png"], "answer_format": "no slot",
            "answer_pattern": "(", "max_tokens": 0}}))
        try:
            HermitAnswer(logs_dir=bad / "logs", plan=str(bad / "plan.json"))
            report["bad"] = "accepted"
        except ValueError as exc:
            report["bad"] = str(exc)

    elif check == "options":
        from harbor.agents.factory import AgentFactory
        from harbor.models.trial.config import AgentConfig
        plan = write_plan(scratch, "codex", deploy=False)
        report["classes"] = {}
        for cli in CLASSES:
            cls = agent_class(cli)
            refused = {}
            for key, kwargs in (("bogus", {"bogus": 1}), ("deploy_dir", {"deploy_dir": "/x"}),
                                ("missing_plan", {"plan": str(scratch / "nowhere.json")})):
                try:
                    cls.parse_options(kwargs, {})
                    refused[key] = "accepted"
                except ValueError as exc:
                    refused[key] = str(exc)
            report["classes"][cli] = {"refused": refused, "name": cls.name(), "import_path": cls.import_path(),
                                      "bin_option": bool(cls.BIN_OPTION) and cls.BIN_OPTION in
                                      cls.options_model.model_fields}
        for name, change in (("version", {"version": 2}), ("cli", {"cli": "claude"}),
                             ("stream", {"stream": "../escape.jsonl"}),
                             ("secret_path", {"home": {"host_dir": str(scratch / "launch" / "home"),
                                                       "secret_files": ["../auth.json"]}}),
                             ("env_name", {"env_from_host": {"NOT A NAME": "X"}})):
            (scratch / f"{name}.json").write_text(json.dumps({**json.loads(plan.read_text()), **change}))
            try:
                make("codex", scratch / f"{name}.json")
                report[name] = "accepted"
            except ValueError as exc:
                report[name] = str(exc)
        try:
            make("codex", plan, base_url="https://api.example.com/v1")
            report["base_url"] = "accepted"
        except ValueError as exc:
            report["base_url"] = str(exc)
        try:                                               # no key in Harbor's environment for this provider
            os.environ.pop("FIREWORKS_API_KEY")
            AgentFactory.run_preflight(AgentConfig(import_path="harborside.codex:HermitCodex",
                                                   model_name="fireworks_ai/accounts/fireworks/models/kimi",
                                                   kwargs={"plan": str(plan)}))
            report["preflight"] = "passed"
        except Exception as exc:                           # noqa: BLE001 - reported to the test
            report["preflight"] = f"{type(exc).__name__}: {exc}"
        connection = make("codex", plan).model_connection
        report["connection"] = [connection.provider, connection.inferred_base_url, connection.api_key]
        report["no_plan"] = run(make("codex"), FakeEnv())

    elif check == "scripted":
        from harborside.scripted import ScriptedAgent
        logs = scratch / "slogs"
        agent = ScriptedAgent(logs_dir=logs, script="echo hi > /app/answer.txt", timeout_sec=5)
        env = FakeEnv()
        answer = env._answer

        async def script_ran(c, user):
            if c.startswith("bash -c"):
                return ExecResult(stdout="script ran\n", stderr="", return_code=3)
            return await answer(c, user)
        env._answer = script_ran
        context = AgentContext()
        asyncio.run(agent.setup(env))
        asyncio.run(agent.run("Write hi.", env, context))
        report["uploads"] = [(c[1], c[2]) for c in calls(env, "upload_file")]
        report["execs"] = [(c[1], c[2], c[3]) for c in env.calls if c[0] == "exec"]
        report["script_txt"] = (logs / "script.txt").read_text()
        report["context"] = context.model_dump()
        report["name"], report["exit_code"] = agent.name(), agent.exit_code
        try:
            ScriptedAgent(logs_dir=logs, script="x", bogus=1)
            report["unknown"] = "accepted"
        except ValueError as exc:
            report["unknown"] = str(exc)

    elif check == "driver_protocol":
        from harborside.driver import StepDriverTrial
        trial = StepDriverTrial.__new__(StepDriverTrial)       # the request handling alone, no Harbor trial behind it
        trial.commands = asyncio.Queue()
        trial.prepared, trial.done = asyncio.Event(), asyncio.Event()
        trial.error, trial.running, trial.next_index = None, None, 1
        trial.aborted, trial.loop_done, trial.finished = None, False, False
        trial.replies_dir = scratch / "replies"
        trial.replies_dir.mkdir()
        trial.paths = SimpleNamespace(trial_dir=scratch / "trial")

        async def scenario():
            out = {"before_ready": await trial.handle({"op": "status"})}
            trial.prepared.set()
            out["ready"] = await trial.handle({"op": "status"})
            out["out_of_order"] = await trial.handle({"op": "step", "name": "s2", "index": 2})
            trial.running = "s1"
            out["busy"] = await trial.handle({"op": "step", "name": "s1", "index": 1})
            trial.running = None
            (trial.replies_dir / "1.json").write_text(json.dumps({"ok": True, "index": 1}))
            out["stored"] = await trial.handle({"op": "result", "index": 1})
            out["missing"] = await trial.handle({"op": "result", "index": 7})
            # A step request is queued for the loop; the loop answers through the future.
            task = asyncio.ensure_future(trial.handle({"op": "step", "name": "s1", "index": 1}))
            await asyncio.sleep(0)
            command, future = await trial.commands.get()
            future.set_result({"ok": True, "index": 1, "step": {}})
            out["queued"] = (command["name"], await task)
            trial.error = "SandboxBuildFailedError: no image"
            out["errored"] = await trial.handle({"op": "step", "name": "s1", "index": 1})
            trial.done.set()
            out["stop_after_error"] = await trial.handle({"op": "stop"})
            trial.error, trial.loop_done = None, True
            out["finished"] = await trial.handle({"op": "step", "name": "s1", "index": 1})
            out["unknown"] = await trial.handle({"op": "nope"})
            return out
        report = asyncio.run(scenario())

    elif check == "driver_plan":
        from harborside.driver import StepDriverTrial
        first = write_plan(scratch / "one", "codex", deploy=False)
        second = write_plan(scratch / "two", "codex", deploy=False, command=["codex", "exec", "second"])
        trial = StepDriverTrial.__new__(StepDriverTrial)       # the step's _apply alone, no Harbor trial behind it
        trial.agent = make("codex", first)
        trial._apply({"name": "s1", "agent_kwargs": {"plan": str(second)}})       # what the stream client sends
        report["from_kwargs"] = trial.agent._plan["command"]
        trial._apply({"name": "s2", "plan": str(first)})
        report["from_request"] = trial.agent._plan["command"]
        trial._apply({"name": "s3", "agent_kwargs": {}})
        report["kept"] = trial.agent._plan["command"]
        trial.agent = SimpleNamespace()                         # an agent with no plan: the step runs as configured
        trial._apply({"name": "s4", "plan": str(first)})
        report["no_plan_agent"] = "ran"

    elif check == "driver_loop":
        import logging
        from harborside.driver import StepDriverTrial
        good = write_plan(scratch / "good", "codex", deploy=False)
        bad = scratch / "bad.json"                              # a plan this agent cannot take: another CLI's
        bad.write_text(json.dumps({**json.loads(good.read_text()), "cli": "claude"}))
        trial = StepDriverTrial.__new__(StepDriverTrial)       # the step loop alone; Harbor's step stands in
        trial.commands, trial.logger = asyncio.Queue(), logging.getLogger("driver_loop")
        trial.running, trial.next_index, trial.aborted, trial.loop_done, trial.finished = None, 1, None, False, False
        trial.replies_dir = scratch / "replies"
        trial.replies_dir.mkdir()
        trial.task = SimpleNamespace(name="demo", config=SimpleNamespace(
            steps=[SimpleNamespace(name="s1", min_reward=None), SimpleNamespace(name="s2", min_reward=None)]))
        trial._result = SimpleNamespace(step_results=None, verifier_result=None)
        trial.paths = SimpleNamespace(trial_dir=scratch / "trial", step_dir=lambda name: scratch / "trial" / name,
                                      cleanup_empty_mount_dirs=lambda: None)
        trial.agent = make("codex", good)
        ran = []

        async def run_step(step, step_result, *, index, total):
            ran.append(step.name)

        async def stop_environment():
            pass
        trial._run_step, trial._stop_agent_environment = run_step, stop_environment
        trial._move_agent_dir_to_step, trial._select_multi_step_reward = (lambda step: None), (lambda: None)

        async def scenario():
            loop = asyncio.ensure_future(trial._run())

            async def ask(command):
                future = asyncio.get_running_loop().create_future()
                await trial.commands.put((command, future))
                try:
                    return await asyncio.wait_for(future, 5)
                except asyncio.TimeoutError:
                    return "no reply"
            out = {"unknown": await ask({"op": "step", "name": "nope", "index": 1})}
            out["next"] = await ask({"op": "step", "name": "s1", "index": 1, "plan": str(good)})
            out["bad_plan"] = await ask({"op": "step", "name": "s2", "index": 2, "plan": str(bad)})
            try:
                await asyncio.wait_for(loop, 5)
            except BaseException as exc:                   # noqa: BLE001 - reported to the test
                out["loop_error"] = f"{type(exc).__name__}: {exc}"
            out["ran"], out["loop_done"] = ran, trial.loop_done
            return out
        report = asyncio.run(scenario())

    elif check == "driver_config":
        import contextlib
        import io
        from harborside import driver
        seen, create = [], driver.StepDriverTrial.create

        async def capture(cls, config):                    # the trial serve() would run: its config, and no more
            seen.append(config)
            raise RuntimeError("no trial here")
        args = ["--task-dir", str(scratch / "task"), "--stream-dir", str(scratch), "--name", "demo__s1",
                "--socket", str(scratch / "driver.sock"),
                "--agent-json", json.dumps({"import_path": "harborside.codex:HermitCodex"})]
        given = {"verifier_timeout_multiplier": 2.5, "environment_build_timeout_multiplier": 3.0}
        driver.StepDriverTrial.create = classmethod(capture)
        try:
            for name, extra in (("given", ["--trial-json", json.dumps(given)]), ("default", []),
                                ("unknown", ["--trial-json", json.dumps({"agent_timeout_multiplier": 2.0})])):
                seen.clear()
                said = io.StringIO()
                try:
                    with contextlib.redirect_stderr(said):
                        code = driver.main(args + extra)
                except SystemExit as exc:                  # argparse refused an argument
                    code = f"SystemExit {exc.code}"
                multipliers = [[c.verifier_timeout_multiplier, c.environment_build_timeout_multiplier] for c in seen]
                report[name] = {"code": code, "log": said.getvalue()[-1000:],
                                "multipliers": multipliers[0] if multipliers else None}
        finally:
            driver.StepDriverTrial.create = create

    else:
        raise ValueError(f"unknown check {check}")
    return report


CHECKS = ("codex_plan", "context_limit", "every_cli", "workdir_pwd", "shell_noise", "slash_prompt", "stream_steps",
          "deploy_failed_step", "cancel_fake", "nonzero_exit", "launch_failures", "capture_fails",
          "launch_exec_failed", "after_cancelled", "after_bounded", "return_check", "workdir_removed", "copy_fails",
          "shell_session", "shell_cancel", "shell_token", "secrets_refused", "installs", "install_failures",
          "mount_rejected", "codex_nvm", "mini_swe_agent", "tool_script", "answer", "options", "scripted",
          "driver_protocol", "driver_plan", "driver_loop", "driver_config")

if __name__ == "__main__":
    sys.path.insert(0, str(HARBORSIDE_ROOT))
    reports = {}
    for name in sys.argv[1].split(","):
        environ = dict(os.environ)
        try:
            reports[name] = harbor_side(name, Path(sys.argv[2]))
        except BaseException:                              # noqa: BLE001 - the test that reads it fails, saying why
            reports[name] = {"check_failed": traceback.format_exc()[-4000:]}
        finally:
            os.environ.clear()
            os.environ.update(environ)
    print(json.dumps(reports, default=str))
    raise SystemExit(0)


# ----- the pytest side ----------------------------------------------------------------------------------------------

import pytest  # noqa: E402  (the Harbor venv has no pytest; the script half above must not need it)

from hermit.harbor import errors  # noqa: E402  (Hermit's side: how it counts what harborside raised)

pytestmark = pytest.mark.skipif(not HARBOR_PYTHON.is_file(), reason=f"needs the Harbor venv at {HARBOR_PYTHON}")


@pytest.fixture(scope="module")
def checks(tmp_path_factory):
    """Every check's report, from one run of this module under the Harbor venv's Python."""
    scratch = tmp_path_factory.mktemp("harborside")
    proc = subprocess.run([str(HARBOR_PYTHON), "-I", str(Path(__file__).resolve()), ",".join(CHECKS), str(scratch)],
                          capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def report(checks: dict, name: str) -> dict:
    got = checks[name]
    assert "check_failed" not in got, got["check_failed"]
    return got


def verdict(raised: list) -> tuple:
    """How Hermit's side counts a trial whose agent raised `raised` ([type, message]): (status, failure cause)."""
    got = errors.classify({"exception_info": {"exception_type": raised[0], "exception_message": raised[1]}})
    return got["status"], got["failure_cause"]


def test_a_codex_plan_is_carried_out_in_order_with_its_files_where_the_cli_reads_them(checks):
    """The deployment (copy, host files, context, record), the home, the prompt and the launch script, the launch;
    afterwards the kill, the copy-back, the collected files and the returned sign-in. Placeholders are filled with
    the workdir in the plan's command and variables and in the home's files, not in its sign-in file."""
    r = report(checks, "codex_plan")
    assert r["raised"] is None
    order = r["order"]
    assert order.index("upload_dir:/app/hermit") < order.index("upload_dir:/app") \
        < order.index("upload_file:/app/AGENTS.md") < order.index("upload_file:/app/.hermit-deploy.json") \
        < order.index("upload_dir:/installed-agent/hermit-home") \
        < order.index("upload_file:/installed-agent/hermit/run/prompt.txt") \
        < order.index("upload_file:/installed-agent/hermit/run/launch.sh") \
        < order.index("compose:exec bash /installed-agent/hermit/run/launch.sh")
    after = order[order.index("compose:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    assert after[0].startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
    assert after[1].startswith("exec:mkdir -p /logs/agent/hermit-copy")
    assert after[2].startswith("exec:mkdir -p /logs/agent/hermit-collect")
    assert after[-1] == "download_file:/installed-agent/hermit-home/auth.json"
    files = r["uploaded"]
    assert files["/app/hermit/demo/ADAPTER.md"] == "The adapter.\n"
    assert files["/app/.codex/hooks.json"] == '{"hooks": {}}' and files["/app/.codex/config.toml"] == "# roles\n"
    assert files["/app/AGENTS.md"] == "Hermit context.\n\n" + IMAGE_CONTEXT          # Hermit's first, the image's after
    assert json.loads(files["/app/.hermit-deploy.json"])["adapter_id"] == "demo"
    home = "/installed-agent/hermit-home"
    assert files[f"{home}/config.toml"] == '[projects."/app"]\ntrust_level = "trusted"\n'
    assert "@@HERMIT_WORKDIR@@ left as it is" in files[f"{home}/auth.json"]
    assert files["/installed-agent/hermit/run/prompt.txt"] == INSTRUCTION + SUFFIX
    script = files["/installed-agent/hermit/run/launch.sh"]
    for line in ("cd -- /app || { echo 'hermit: cannot enter the workdir /app' >&2; exit 97; }",
                 "export CODEX_HOME=/installed-agent/hermit-home", "export PROJECT=/app/src",
                 'export PATH=/opt/hermit-codex/bin:"$PATH"', "export HERMIT_HOOK_LOGS=/logs/agent",
                 'export OPENAI_API_KEY="${FIREWORKS_API_KEY-}"',
                 "IFS= read -r -d '' HERMIT_PROMPT_TEXT < /installed-agent/hermit/run/prompt.txt || true",
                 '  setsid codex exec -C /app "$HERMIT_PROMPT_TEXT" > /logs/agent/agent-codex.jsonl 2>&1 < /dev/null &',
                 'echo "$pid" > /installed-agent/hermit/run/session.pid'):
        assert line in script.splitlines(), line
    assert "unset FIREWORKS_API_KEY" not in script                     # also passed under its own name
    roots = [e[1] for e in r["execs"] if e[0] == "root"]
    assert "rm -rf /app/hermit/demo && mkdir -p /app/hermit" in roots                  # a replaced copy
    assert ("chown -R 1000:1000 /app/hermit/demo /app/.codex /app/AGENTS.md /app/.hermit-deploy.json && "
            "chmod -R u+rwX /app/hermit/demo /app/.codex /app/AGENTS.md /app/.hermit-deploy.json") in roots
    assert (f"chown -R 1000:1000 {home} && chmod -R u+rwX,go-rwx {home} && chmod 600 {home}/auth.json") in roots
    copy = next(c for c in roots if "hermit-copy" in c)
    assert ("then cp -a /app/hermit/demo/memory /logs/agent/hermit-copy/memory || { f=1; rm -rf "
            "/logs/agent/hermit-copy/memory; }; fi") in copy
    collect = next(c for c in roots if "hermit-collect" in c)
    assert (f"then cp {home}/last_message.txt /logs/agent/hermit-collect/last_message.txt || {{ f=1; rm -rf "
            "/logs/agent/hermit-collect/last_message.txt; }; fi") in collect
    assert "auth.json" not in collect                                                  # a sign-in never goes to logs
    assert r["returned"] == f"downloaded {home}/auth.json"
    assert r["context"]["metadata"] == {
        "plan_digest": "abc123", "hermit_cli": "codex", "adapter_deployed": True, "adapter_id": "demo",
        "hooks_active": True, "python3": True, "mounted": False, "exit_code": 0, "killed": False,
        "stream": "agent-codex.jsonl"}
    assert r["context"]["n_input_tokens"] is None                                      # Hermit reads the stream


def test_a_link_in_the_staging_is_uploaded_as_a_link_never_followed_to_a_host_file(checks):
    """An evolved adapter's deployment or a rendered home could hold a link to any file of this host: it reaches the
    container as the link it is (as `docker cp` keeps the adapter's own copy), its target is neither read nor
    written (no placeholder filled through it), and a link at the top of the workdir is not chown'd as root."""
    r = report(checks, "codex_plan")
    files = r["uploaded"]
    assert files["/app/.codex/linked.txt"] == f"<link to {r['canary']}>"
    assert files["/app/linked-dir"] == f"<link to {r['scratch']}>"
    assert files["/installed-agent/hermit-home/linked.toml"] == f"<link to {r['canary']}>"
    assert not any("a host file" in text for text in files.values())
    assert r["canary_after"] == "a host file, @@HERMIT_WORKDIR@@\n"
    assert not any("/app/linked-dir" in c for user, c in r["execs"] if user == "root" and c.startswith("chown"))


def test_a_secret_goes_to_compose_by_name_and_is_in_no_argument_file_or_log(checks):
    """FIREWORKS_API_KEY's value is in Harbor's environment only: the compose call names it, the compose client
    resolves it, and no recorded exec, upload, compose argument or download holds the value."""
    r = report(checks, "codex_plan")
    assert r["leaks"] == []
    assert r["compose"] == [["exec", "-w", "/app", "-e", "FIREWORKS_API_KEY", "main", "bash", "-c",
                             "exec bash /installed-agent/hermit/run/launch.sh"]]
    assert r["compose_names_resolved"] == [{"FIREWORKS_API_KEY": True}]


def test_secrets_by_name_are_refused_clearly_outside_docker_or_when_harbor_lacks_them(checks):
    r = report(checks, "secrets_refused")
    assert r["plain_env"][0] == "HermitLaunchError" and "only a local Docker environment does" in r["plain_env"][1]
    assert r["missing"][0] == "HermitLaunchError"
    assert "FIREWORKS_API_KEY is not in Harbor's environment" in r["missing"][1]
    assert r["missing_compose"] == []                                                   # nothing was started


def test_a_context_over_the_clis_limit_fails_the_trial_before_the_session_and_one_within_is_merged(checks):
    """dsh drops AGENTS.md past its limit without saying so: the merged file (Hermit's, then the image's own) is
    checked against the plan's context_limit before it is written, and the after-steps still run."""
    over, within = report(checks, "context_limit")["over"], report(checks, "context_limit")["within"]
    assert over["raised"] == ["HermitContextLimitError",
                              "hermit: AGENTS.md for dsh would be 66 bytes (Hermit's context 16, then the image's own "
                              "49), over the CLI's limit of 60 bytes: shorten the adapter's context"]
    assert "/app/AGENTS.md" not in over["uploaded"]
    assert not any("launch.sh" in step and step.startswith("exec:exec") for step in over["order"])
    kill, copy, collect = over["order"][-3:]                                            # the after-steps still ran
    assert kill.startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
    assert copy.startswith("exec:mkdir -p /logs/agent/hermit-copy;")
    assert collect.startswith("exec:mkdir -p /logs/agent/hermit-collect;")
    assert within["raised"] is None and within["uploaded"]["/app/AGENTS.md"] == "Hermit context.\n\n" + IMAGE_CONTEXT


def test_every_cli_gets_the_deployment_record_hook_logs_and_its_prompt_where_the_plan_says(checks):
    """The record (an adapter's hook runner finds its host there) is uploaded for every CLI, not only Claude Code's,
    and so is HERMIT_HOOK_LOGS set; the prompt goes on stdin or into the argument the plan marks."""
    r = report(checks, "every_cli")
    for cli, got in r.items():
        assert got["raised"] is None, (cli, got["raised"])
        assert json.loads(got["record"])["adapter_id"] == "demo", cli
        assert "export HERMIT_HOOK_LOGS=/logs/agent" in got["launch"].splitlines(), cli
        assert got["prompt"] == INSTRUCTION + SUFFIX, cli
        stdin = "< /installed-agent/hermit/run/prompt.txt &" in got["launch"]
        assert stdin == (cli in ("claude", "opencode", "mini-swe-agent")), cli
        assert f"> /logs/agent/agent-{cli}.jsonl 2>&1" in got["launch"], cli
        for rel in HOST_FILES[cli]:
            assert f"/app/{rel}" in got["workdir_files"], (cli, rel)
        if CONTEXT_FILE[cli]:
            assert got["context"] == "Hermit context.\n\n" + IMAGE_CONTEXT, cli
    assert f"  setsid {TOOL_PYTHON} exec -C /app" in r["mini-swe-agent"]["launch"]       # its own Python


def test_a_plan_without_a_workdir_runs_where_harbors_execs_start(checks):
    """A task.toml that names no workdir: the session, the deployment and the placeholders use `pwd` as the task's
    agent user (the image's WORKDIR)."""
    r = report(checks, "workdir_pwd")
    assert r["raised"] is None and r["pwd_user"] == [None]
    files = r["uploaded"]
    assert "/testbed/hermit/demo/ADAPTER.md" in files and "/testbed/.hermit-deploy.json" in files
    assert files["/testbed/AGENTS.md"] == "Hermit context.\n"                          # the image had none
    assert files["/installed-agent/hermit-home/config.toml"].startswith('[projects."/testbed"]')
    script = files["/installed-agent/hermit/run/launch.sh"].splitlines()
    assert "cd -- /testbed || { echo 'hermit: cannot enter the workdir /testbed' >&2; exit 97; }" in script
    assert "export PROJECT=/testbed/src" in script


def test_a_workdir_of_the_root_gets_the_deployment_in_the_root(checks):
    """No workdir in the task and none in the image: Harbor's execs start in /, and the deployment's files go to /,
    not to an empty container path."""
    r = report(checks, "workdir_pwd")
    assert r["root_raised"] is None
    assert r["root_dirs"] == ["/hermit", "/", "/installed-agent/hermit-home"]
    assert any(f.startswith("/AGENTS.md Hermit context.") for f in r["root_files"])
    assert any(f.startswith("/.hermit-deploy.json ") for f in r["root_files"])


def test_the_agent_user_the_workdir_and_the_images_context_are_read_among_other_lines_of_the_exec(checks):
    """Harbor's docker exec merges stderr into stdout, and an image's BASH_ENV may echo: each value read from the
    container is found on a marked line of its own among other lines before and after it, for real with bash (the
    trial is not refused for its agent user, and the image's context file is merged as it is, without the noise)."""
    r = report(checks, "shell_noise")
    assert r["raised"] is None
    assert r["context"] == "Hermit context.\n\n" + IMAGE_CONTEXT
    assert any(c.startswith(f"chown -R {r['owner']} ") for c in r["chowns"])
    assert any(line.startswith(f"cd -- {r['root']}/app ||") for line in r["launch"].splitlines())
    assert f"pwd: {r['root']}/app" in r["stream"].splitlines()                         # where the CLI ran


def test_a_prompt_that_starts_with_a_slash_gets_the_plans_prefix(checks):
    """Claude Code runs a prompt that starts with `/` as one of its commands: the plan's prompt_slash_prefix (its Base
    Agent's line break) goes before such an instruction; a plan without one passes it as it is."""
    r = report(checks, "slash_prompt")
    assert r["guarded"]["raised"] is None and r["plain"]["raised"] is None
    assert r["guarded"]["prompt"] == "\n/app/data.csv holds the rows." + SUFFIX
    assert r["plain"]["prompt"] == "/app/data.csv holds the rows." + SUFFIX


def test_a_streams_later_step_replaces_the_deployment_and_one_without_restores_the_image(checks):
    """One container, three steps: the second's context is merged with the image's own file (captured before the
    first step), not with the first step's; what the first deployed and the second does not is removed; a step with
    no deployment puts the image's context file back and removes the rest."""
    r = report(checks, "stream_steps")
    one, two, three = r["one"], r["two"], r["three"]
    assert one["raised"] is None and two["raised"] is None and three["raised"] is None
    assert one["uploaded"]["/app/AGENTS.md"] == "Hermit context.\n\n" + IMAGE_CONTEXT
    assert two["uploaded"]["/app/AGENTS.md"] == "Hermit context, revised.\n\n" + IMAGE_CONTEXT
    assert "rm -rf /app/.codex/hooks.json" in two["root_execs"]
    assert "/app/.codex/hooks.json" not in two["uploaded"]
    assert ("rm -rf /app/.codex/config.toml /app/hermit/demo /app/.hermit-deploy.json && if [ -f "
            "/installed-agent/hermit/original-AGENTS.md ]; then cat /installed-agent/hermit/original-AGENTS.md > "
            "/app/AGENTS.md; else rm -f /app/AGENTS.md; fi") in three["root_execs"]
    assert not any(k.startswith("/app/") for k in three["uploaded"])


def test_a_streams_step_whose_deployment_failed_does_not_report_the_hooks_of_the_step_before(checks):
    """One container, two steps: the first deploys an adapter whose hooks are active; the second deploys none, and
    removing what the first one deployed fails (HermitLaunchError). The second step's metadata says that no hooks were
    active, not the first step's value."""
    r = report(checks, "deploy_failed_step")
    one, two = r["one"], r["two"]
    assert one["raised"] is None and one["metadata"]["hooks_active"] is True
    assert two["raised"][0] == "HermitLaunchError"
    assert two["raised"][1].startswith("hermit: deploying the adapter failed: ")
    assert two["metadata"]["hooks_active"] is False and two["metadata"]["adapter_deployed"] is False


def test_a_session_harbors_timeout_cancels_is_killed_even_without_an_adapter(checks):
    """Harbor's timeout cancels only its wait on the compose client, and the CLI goes on in the container: the
    session's process group is killed in the finally (TERM, the plan's grace, KILL) as root from its pid file, with no
    adapter deployed; the metadata says it was killed."""
    r = report(checks, "cancel_fake")
    assert r["raised"][0] == "TimeoutError"
    assert len(r["kill"]) == 1 and r["kill"][0][0] == "root"
    assert 'kill -TERM -- -"$p"' in r["kill"][0][1] and 'kill -KILL -- -"$p"' in r["kill"][0][1]
    assert 'while [ "$i" -lt 2 ]' in r["kill"][0][1]                                    # the plan's grace
    assert r["metadata"]["killed"] is True and r["metadata"]["exit_code"] is None
    assert r["metadata"]["adapter_deployed"] is False and r["metadata"]["hermit_cli"] == "pi"


def test_a_cli_that_exits_non_zero_raises_harbors_agent_error_and_a_failed_copy_back_does_not_hide_it(checks):
    """NonZeroAgentExitCodeError lets the verifier run (Hermit's side reads the verdict from the stream, which the
    launch script made); the copy-back that failed after it is recorded in the metadata, and the steps after it still
    ran."""
    r = report(checks, "nonzero_exit")
    assert r["raised"] == ["NonZeroAgentExitCodeError", "hermit: pi exited with 3 (its stream: "
                           "/logs/agent/agent-pi.jsonl)"]
    after = r["order"][r["order"].index("exec:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    assert after[0] == "exec:test -e /logs/agent/agent-pi.jsonl"                         # the stream is there
    assert after[1].startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
    assert after[2].startswith("exec:mkdir -p /logs/agent/hermit-copy;")                 # it failed
    assert after[3].startswith("exec:mkdir -p /logs/agent/hermit-collect;")              # and the next one ran
    assert r["metadata"]["exit_code"] == 3 and r["metadata"]["killed"] is False
    assert [e.split(":")[0] for e in r["metadata"]["after_session_errors"]] == ["copy_back"]


def test_harborsides_own_failures_before_the_session_are_launch_failures_not_the_agents_zero(checks):
    """A chown of the deployment or the probe of the task's agent user failing, or the launch script unable to enter
    the workdir (its exit 97 with its own words, for real with bash), is harborside's, not the agent's: it raises
    HermitLaunchError saying what failed, which Hermit's side counts as infra (launch_failed), and the after-steps
    still run. So does an image's context file that cannot be read (no base64 there): it is never taken for none and
    overwritten with the adapter's alone. A CLI that exits 97 by itself stays the agent's
    (NonZeroAgentExitCodeError)."""
    r = report(checks, "launch_failures")
    chown, owner, enter, own = r["chown"], r["owner"], r["cannot_enter"], r["own_97"]
    assert r["no_base64"]["raised"][0] == "HermitLaunchError"
    assert r["no_base64"]["raised"][1].startswith("hermit: deploying the adapter failed: NonZeroAgentExitCodeError: ")
    assert verdict(r["no_base64"]["raised"]) == ("infra", "launch_failed")
    assert not any(step.startswith("exec:exec bash") for step in r["no_base64"]["order"])
    assert chown["raised"][0] == "HermitLaunchError"
    assert chown["raised"][1].startswith("hermit: deploying the adapter failed: NonZeroAgentExitCodeError: Command "
                                         "failed (exit 1): chown -R 1000:1000 /app/hermit/demo ")
    assert owner["raised"][0] == "HermitLaunchError"
    assert owner["raised"][1].startswith("hermit: reading the task's agent user failed: NonZeroAgentExitCodeError: "
                                         'Command failed (exit 1): echo "hermit-owner:$(id -u):$(id -g)"')
    assert enter["raised"][0] == "HermitLaunchError"
    assert enter["raised"][1].startswith("hermit: codex did not start: ")
    assert "hermit: cannot enter the workdir " in enter["raised"][1]
    for got in (chown, owner, enter):
        assert verdict(got["raised"]) == ("infra", "launch_failed")
    for got in (chown, owner):                                 # no session; the after-steps ran
        assert not any(step.startswith("exec:exec bash") for step in got["order"])
        kill, copy, collect = got["order"][-3:]
        assert kill.startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
        assert copy.startswith("exec:mkdir -p /logs/agent/hermit-copy;")
        assert collect.startswith("exec:mkdir -p /logs/agent/hermit-collect;")
    assert own["raised"] == ["NonZeroAgentExitCodeError", "hermit: codex exited with 97 (its stream: "
                             "/logs/agent/agent-codex.jsonl)"]
    assert verdict(own["raised"]) == ("agent_error", "unclassified")


def test_an_images_context_file_whose_read_fails_is_a_launch_failure_never_taken_for_none_and_overwritten(checks):
    """For real with bash, the image's own AGENTS.md and a base64 that fails reading it (base64 and tr are both
    there): the capture exits saying so instead of printing an empty value, so the trial is HermitLaunchError (infra,
    launch_failed) before any session, and the image's file is left as it was, not replaced by the adapter's alone."""
    got = report(checks, "launch_failures")["read_fails"]
    raised = got["raised"]
    assert raised is not None and raised[0] == "HermitLaunchError", raised
    assert raised[1].startswith("hermit: deploying the adapter failed: NonZeroAgentExitCodeError: Command failed "
                                "(exit 3): ")
    assert "base64: read error: Input/output error" in raised[1]
    assert verdict(raised) == ("infra", "launch_failed")
    assert got["context"] == IMAGE_CONTEXT
    assert not any(step.startswith("exec:exec bash") for step in got["order"])


def test_an_images_context_file_whose_capture_failed_part_way_is_a_launch_failure_never_merged_or_put_back(checks):
    """For real with bash, one container and three steps of a stream: the first step's capture of the image's own
    AGENTS.md meets a cat that fails part-way, as on a full disk. That step is HermitLaunchError saying why (infra,
    launch_failed) before any session, the image's file left as it was and no part of it kept as the image's own. The
    next step, the cat working again, captures and merges the whole file, and a step without a deployment puts the
    whole file back."""
    r = report(checks, "capture_fails")
    failed, again, restored = r["failed"], r["again"], r["restored"]
    raised = failed["raised"]
    assert raised is not None and raised[0] == "HermitLaunchError", raised
    assert raised[1].startswith("hermit: deploying the adapter failed: NonZeroAgentExitCodeError: Command failed "
                                "(exit 3): ")
    assert "cat: write error: No space left on device" in raised[1]
    assert verdict(raised) == ("infra", "launch_failed")
    assert failed["launched"] is False and failed["context"] == IMAGE_CONTEXT and failed["kept"] is None
    assert again["raised"] is None and again["launched"] is True
    assert again["context"] == "Hermit context.\n\n" + IMAGE_CONTEXT and again["kept"] == IMAGE_CONTEXT
    assert restored["raised"] is None and restored["context"] == IMAGE_CONTEXT


def test_a_launch_that_left_no_stream_is_a_launch_failure_and_one_that_left_a_stream_even_empty_is_the_agents(checks):
    """The launch script's redirect makes the stream before the CLI starts. A non-zero exit with no stream is an exec
    that failed in Docker before the script ran (runc could not start it: exit 126, the stream really absent, for real
    with bash; compose's service had stopped, its probe failing as well): HermitLaunchError, which Hermit's side
    counts as infra (launch_failed), not the agent's 0 on an untouched workspace. A stream that is there, even an
    empty one (for real with bash, a CLI that exits 3 at once and prints nothing), keeps the exit the agent's
    (NonZeroAgentExitCodeError), for Hermit's side to read; so it does when the CLI removed its workdir (the stream is
    looked for from /)."""
    r = report(checks, "launch_exec_failed")
    runc, stopped, empty, gone = r["runc"], r["stopped"], r["empty"], r["workdir_gone"]
    assert runc["raised"] == ["HermitLaunchError", "hermit: codex did not start: its exec exited with 126 and left no "
                              "stream (/logs/agent/agent-codex.jsonl)\nOCI runtime exec failed: exec failed: unable "
                              "to start container process: open /tmp/runc-process1: no space left on device: unknown"]
    assert runc["stream"] is False
    after = runc["order"][runc["order"].index("exec:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    assert after[0] == "exec:test -e /logs/agent/agent-codex.jsonl"
    assert after[1].startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")  # the after-steps still ran
    assert stopped["raised"] == ["HermitLaunchError", "hermit: codex did not start: its exec exited with 1 and left no "
                                 'stream (/logs/agent/agent-codex.jsonl)\nservice "main" is not running']
    for got in (runc, stopped):
        assert verdict(got["raised"]) == ("infra", "launch_failed")
    assert empty["raised"] == ["NonZeroAgentExitCodeError", "hermit: codex exited with 3 (its stream: "
                               "/logs/agent/agent-codex.jsonl)"]
    assert empty["stream"] == ""
    assert verdict(empty["raised"]) == ("agent_error", "unclassified")
    assert gone["workdir"] is False
    assert gone["raised"] == ["NonZeroAgentExitCodeError", "hermit: codex exited with 3 (its stream: "
                              "/logs/agent/agent-codex.jsonl)"]


def test_a_streams_later_step_whose_exec_failed_in_docker_is_a_launch_failure_though_the_step_before_left_a_stream(
        checks):
    """One container, two steps of a stream: the first runs for real with bash and leaves its stream in /logs/agent;
    the second's exec fails as runc's does (exit 126, its launch script not run). The stream an earlier step left is
    removed before the exec, so this step's exec left none: HermitLaunchError, which Hermit's side counts as infra
    (launch_failed), not the agent's exit read from the earlier step's stream."""
    r = report(checks, "launch_exec_failed")["later_step"]
    assert r["first"] is None and r["left"] == "the first step\n"
    assert r["raised"] == ["HermitLaunchError", "hermit: codex did not start: its exec exited with 126 and left no "
                           "stream (/logs/agent/agent-codex.jsonl)\nOCI runtime exec failed: exec failed: unable to "
                           "start container process: open /tmp/runc-process1: no space left on device: unknown"]
    assert verdict(r["raised"]) == ("infra", "launch_failed") and r["stream"] is False


def test_a_session_exec_or_a_look_for_its_stream_this_machine_could_not_spawn_is_a_launch_failure(checks):
    """The compose client of the session's exec (or, after a non-zero exit, of the look for its stream) that this
    machine could not spawn (an OSError from the exec itself: EMFILE, EAGAIN on a loaded host), the exec by name too:
    HermitLaunchError saying what failed, which Hermit's side counts as infra (launch_failed), not an unclassified
    agent error scored 0; the after-steps still run."""
    r = report(checks, "launch_exec_failed")
    for name, what, number in (
            ("unspawned", "codex did not start: its exec could not be run here", errno.EMFILE),
            ("unspawned_by_name", "codex did not start: its exec could not be run here", errno.EAGAIN),
            ("probe_unspawned", "codex exited with 3, and its stream (/logs/agent/agent-codex.jsonl) could not be "
                                "looked for here", errno.EAGAIN)):
        got, raised = r[name], OSError(number, os.strerror(number))     # as Python raises it: EAGAIN's BlockingIOError
        assert got["raised"] == ["HermitLaunchError", f"hermit: {what}: {type(raised).__name__}: {raised}"], \
            (name, got["raised"])
        assert verdict(got["raised"]) == ("infra", "launch_failed"), name
        kill, collect = got["order"][-2:]
        assert kill.startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid"), name
        assert collect.startswith("exec:mkdir -p /logs/agent/hermit-collect;"), name


def test_harbors_timeout_landing_in_an_after_step_still_runs_the_rest_and_then_cancels(checks):
    """The timeout cancels the copy-back of a session that has just ended: the copy-back is not run again (a second
    `rm -rf` then `cp -a` could nest its copy inside the cancelled one's, still running in the container), the
    collected files and the refreshed sign-in still come back (ChatGPT rotates its refresh tokens), the cancellation is
    recorded, and it goes on after the last step, so Harbor still sees its timeout."""
    r = report(checks, "after_cancelled")
    assert r["raised"][0] == "TimeoutError"
    after = r["order"][r["order"].index("exec:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    assert after[0].startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
    assert after[1].startswith("exec:mkdir -p /logs/agent/hermit-copy;")                 # cancelled, not run again
    assert after[2].startswith("exec:mkdir -p /logs/agent/hermit-collect;")              # and the rest still ran
    assert after[-1] == "download_file:/installed-agent/hermit-home/auth.json"
    assert r["returned"] == "downloaded /installed-agent/hermit-home/auth.json"
    assert r["metadata"]["after_session_errors"] == ["copy_back: cancelled"]


def test_harbors_timeout_landing_in_the_return_of_the_files_still_brings_the_refreshed_sign_in_back(checks):
    """The timeout lands while the refreshed sign-in of a session that has just ended is returned (its check of the
    file, in the last after-step): that step runs once more, so the sign-in still comes back to the host (ChatGPT
    rotates its refresh tokens: the old one would be stale), and the cancellation goes on after it, so Harbor still
    sees its timeout."""
    r = report(checks, "after_cancelled")["in_return"]
    assert r["raised"][0] == "TimeoutError"
    after = r["order"][r["order"].index("exec:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    check = "exec:test -f /installed-agent/hermit-home/auth.json && test ! -L "
    assert after[2].startswith("exec:mkdir -p /logs/agent/hermit-collect;")
    assert [step.startswith(check) for step in after[3:5]] == [True, True], after        # cancelled, then once more
    assert after[5:] == ["download_file:/installed-agent/hermit-home/auth.json"]
    assert r["returned"] == "downloaded /installed-agent/hermit-home/auth.json"
    assert r["metadata"]["after_session_errors"] == ["return_files: cancelled"]


def test_once_the_run_is_cancelled_each_after_step_has_a_bounded_time_and_the_files_still_come_back(checks):
    """Harbor's timeout cancels the session, and the container hangs after it: the copy-back and the collect are each
    cut short at AFTER_CANCEL_S (0.3 s here) and recorded so, the refreshed sign-in still comes back, and Harbor sees
    its timeout in a few seconds, not after every step has waited as long as the container hangs (5 s each here)."""
    r = report(checks, "after_bounded")["bounded"]
    assert r["raised"][0] == "TimeoutError" and r["seconds"] < 4
    assert r["metadata"]["after_session_errors"] == ["copy_back: not done within 0.3 s of the run's cancellation",
                                                     "collect: not done within 0.3 s of the run's cancellation"]
    assert r["metadata"]["killed"] is True
    assert r["order"][-1] == "download_file:/installed-agent/hermit-home/auth.json"
    assert r["returned"] == "downloaded /installed-agent/hermit-home/auth.json"


def test_a_second_cancellation_ends_the_run_at_once_without_the_after_steps_left(checks):
    """The job cancelled after Harbor's timeout (a second cancellation, while the copy-back hangs): the run ends at
    once with it, and the steps after the copy-back (the collected files, the returned ones) are not run."""
    r = report(checks, "after_bounded")["twice"]
    assert r["raised"][0] == "CancelledError" and r["seconds"] < 2
    after = r["order"][r["order"].index("exec:exec bash /installed-agent/hermit/run/launch.sh") + 1:]
    assert len(after) == 2, after
    assert after[0].startswith("exec:p=$(cat /installed-agent/hermit/run/session.pid")
    assert after[1].startswith("exec:mkdir -p /logs/agent/hermit-copy;")


def test_after_a_session_removed_its_workdir_the_after_steps_still_kill_it_collect_and_return_the_sign_in(checks):
    """Docker refuses an exec in a workdir the session removed (exit 126; ShellEnv, for real with bash, likewise):
    the kill, the copy-back, the collect and the return run from /, so a session that removed its workdir (and the
    deployment in it) and ran on until Harbor's timeout is killed with the child it started, and whether it ended by
    itself or was cancelled, the copy-back comes back (its folder, empty: the memory went with the workdir), its last
    message is collected and its refreshed sign-in comes back to the host (ChatGPT rotates its refresh tokens), none
    of it failing."""
    r = report(checks, "workdir_removed")
    ended, running = r["ended"], r["running"]
    for name, got in r.items():
        assert got["workdir"] is False, name
        assert got["returned"] == '{"refreshed": true}\n', name
        assert got["copied"] == [], name
        assert got["collected"] == ["last_message.txt"], name
        assert "after_session_errors" not in got["metadata"], (name, got["metadata"])
    assert ended["raised"] is None and ended["metadata"]["killed"] is False
    assert running["raised"][0] == "CancelledError"
    assert running["metadata"]["killed"] is True and running["child_alive"] is False


def test_a_return_whose_check_failed_is_recorded_and_a_file_the_session_did_not_leave_is_skipped(checks):
    """The check of a file to return fails in a container whose compose service had stopped: the failure is recorded
    with the after-steps' errors, saying so, not taken for a file the session did not leave (a refreshed sign-in that
    never came back, unseen). A file the session did not leave is skipped, and nothing is recorded."""
    r = report(checks, "return_check")
    failed, absent = r["failed"], r["absent"]
    assert failed["raised"] is None and failed["downloads"] == [] and failed["returned"] is False
    assert failed["metadata"]["after_session_errors"] == [
        "return_files: whether the session left /installed-agent/hermit-home/auth.json could not be told (exit 1): "
        'service "main" is not running']
    assert absent["raised"] is None and absent["downloads"] == [] and absent["returned"] is False
    assert "after_session_errors" not in absent["metadata"]


def test_a_copy_back_or_collect_that_failed_part_way_is_recorded_and_removed_and_the_parts_after_it_come_back(checks):
    """For real with bash, a cp that fails part-way, as on a full disk (the first bytes of each file written, mode
    600, then exit 1), copying the adapter's memory in the copy-back and last_message.txt and a sessions folder in the
    collect: each copy that failed is removed, never kept in part (Hermit's side would take a cut last message for the
    reply, where without the file it reads the stream's, and a cut memory for the adapter's), and its step fails,
    recorded with the after-steps' errors saying why, the session's own outcome unchanged. The part after them in each
    step (a folder of the adapter's own, the home's agents folder) is still copied, whole, and the chmod after them
    still ran, so what was copied is readable: each file there, mode 600 where it was copied from, comes back 644."""
    r = report(checks, "copy_fails")
    assert r["raised"] is None
    failed = r["metadata"]["after_session_errors"]
    assert [e.split(":")[0] for e in failed] == ["copy_back", "collect"], failed
    for error in failed:
        assert "Command failed (exit 1): " in error and "No space left on device" in error, error
    assert r["entries"] == ["hermit-collect/agents", "hermit-collect/agents/reviewer.toml", "hermit-copy/journal",
                            "hermit-copy/journal/JOURNAL.md"]
    assert r["source_modes"] == {"hermit-copy/journal/JOURNAL.md": "0o600",
                                 "hermit-collect/agents/reviewer.toml": "0o600"}
    assert r["kept"] == {"hermit-copy/journal/JOURNAL.md": ["# Journal\n", "0o644"],
                         "hermit-collect/agents/reviewer.toml": ['name = "reviewer"\n', "0o644"]}


def test_the_launch_script_runs_the_cli_with_its_prompt_variables_and_secrets_and_brings_its_files_back(checks):
    """For real, with bash and a stand-in CLI: the prompt arrives byte for byte (quotes, $HOME, newlines), the
    workdir, the hook log directory, the key by name (also under its second name), a process group of the session's
    own; afterwards the memory is copied back, last_message.txt collected (the sign-in is not), the refreshed sign-in
    returned to the host, the home's files private, and the pid file gone."""
    r = report(checks, "shell_session")
    root = r["root"]
    assert r["raised"] is None
    lines = r["stream"].split("\n")
    assert r["stream"].startswith(f"argv: <exec> <-C> <{root}/app> <{INSTRUCTION + SUFFIX}>\n")
    for line in ("stdin: <>", f"pwd: {root}/app", f"hook-logs: {root}/logs/agent", "key-in-env: yes",
                 "renamed-key: yes", "token-in-env: ", "pgid-is-own: yes"):
        assert line in lines, line
    assert r["copied_notes"] == "# Notes\nlearned something\n"
    assert r["collected"] == ["last_message.txt"]
    assert r["returned"] == '{"refreshed": true}\n'
    assert r["home_modes"]["auth.json"] == "0o600" and r["home_modes"]["config.toml"] == "0o600"
    assert r["home_config"].startswith('[projects."/app"]')
    assert r["pidfile_left"] is False and r["leaks"] == []
    assert r["metadata"]["exit_code"] == 0 and r["metadata"]["killed"] is False


def test_harbors_timeout_kills_the_real_session_and_what_it_started_and_the_files_still_come_back(checks):
    """The run cancelled as Harbor's timeout cancels it, while the stand-in CLI waits on a child of its own: the
    kill reaches the child (the group, not just the CLI), and the collected and returned files still come back."""
    r = report(checks, "shell_cancel")
    assert r["raised"][0] == "CancelledError" and r["seconds"] < 30
    assert r["child_alive"] is False                                                    # the group, not just the CLI
    assert r["metadata"]["killed"] is True and r["pidfile_left"] is False
    assert r["collected"] == ["last_message.txt"] and r["returned"] == '{"refreshed": true}\n'


def test_the_subscription_token_reaches_the_cli_on_its_descriptor_and_in_no_environment(checks):
    """Claude Code's env_by_fd token: by name to compose, into a pipe on descriptor 3, out of the environment of the
    CLI and of the shell that started it (the one that had it was replaced), never in an argument or a file."""
    r = report(checks, "shell_token")
    assert r["raised"] is None
    lines = r["stream"].split("\n")
    for line in (f"fd-sha: {r['token_sha']}", "token-in-env: ", "parent-environ-token: 0",
                 "argv: <-p> <--output-format> <stream-json>"):
        assert line in lines, line
    assert f"stdin: <{INSTRUCTION + SUFFIX}>" in r["stream"]
    assert r["leaks"] == []


def test_each_class_installs_its_cli_or_uses_a_mounted_binary_of_the_asked_for_version(checks):
    r = report(checks, "installs")
    for name, got in r.items():
        if isinstance(got, dict) and "raised" in got:
            assert got["raised"] is None, (name, got["raised"])

    def root(name):
        return [e[1] for e in r[name]["execs"] if e[0] == "root"]

    def agent(name):
        return [e[1] for e in r[name]["execs"] if e[0] is None]

    assert r["codex_mounted"]["mounted"] is True
    assert "mkdir -p /usr/local/bin && ln -sf /opt/hermit-codex/bin/codex /usr/local/bin/codex" in root("codex_mounted")
    assert any("[ -x /opt/hermit-codex/codex-path/rg ]" in c for c in root("codex_mounted"))
    assert r["codex_mounted"]["uploads"] == [] and not any("npm" in c for c in agent("codex_mounted"))
    assert r["codex_bare"]["uploads"] == ["/etc/ssl/certs/ca-certificates.crt"]       # the host's CA bundle
    assert r["codex_other"]["mounted"] is False
    assert any("npm install -g @openai/codex@0.164.0" in c for c in agent("codex_other"))
    assert r["claude_mounted"]["mounted"] is True
    assert "mkdir -p /usr/local/bin && ln -sf /opt/hermit-claude/claude /usr/local/bin/claude" in root("claude_mounted")
    assert any("bootstrap.sh | bash -s -- 2.1.318" in c for c in agent("claude"))
    assert any("npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.0" in c for c in agent("pi"))
    assert r["pi_mounted"]["mounted"] is True
    assert "mkdir -p /usr/local/bin && ln -sf /opt/hermit-pi/pi /usr/local/bin/pi" in root("pi_mounted")
    opencode = next(c for c in root("opencode") if "opencode-linux" in c)
    assert 'url="https://github.com/anomalyco/opencode/releases/download/v2.2.6/$asset"' in opencode
    assert not any("apt-get install" in c for c in root("opencode"))
    assert "apt-get update && apt-get install -y curl ca-certificates" in root("opencode_bare")
    assert r["opencode_mounted"]["mounted"] is True
    pattern = "https://github.com/anomalyco/opencode/releases/download/v2.2.6/opencode-linux-{}.tar.gz"
    assert r["opencode_url_musl"] in (pattern.format("x64-musl"), pattern.format("arm64-musl"))
    assert r["opencode_url_glibc"] in (pattern.format("x64"), pattern.format("arm64"))
    dsh = next(c for c in agent("dsh") if "@deepseek-ai/dsh" in c)
    assert "nvm install 24 && nvm alias default 24" in dsh and "npm install -g @deepseek-ai/dsh@0.2.2-rc.1" in dsh
    assert "if (a < 24 || (a === 24 && b < 9))" in dsh                                # Node 24.9 or later
    assert r["dsh_musl"]["code"] == 1 and "this image is musl" in r["dsh_musl"]["stderr"]
    assert r["dsh_musl"]["curl"] == ""                                                  # refused before any download
    assert "https://raw.githubusercontent.com/nvm-sh/nvm/" in r["dsh_glibc"]["curl"]    # glibc: on to nvm's installer
    assert r["dsh_mounted"]["mounted"] is True
    assert any("uv tool install --python 3.12 mini-swe-agent==2.4.6" in c for c in agent("mini"))
    assert r["setup_timeouts"]["dsh"] == 600.0
    assert r["path_unmounted"] == ['export PATH=/x/bin:"$PATH"']
    assert r["path_mounted"] == ['export PATH=/x/bin:"$PATH"', 'export PATH=/opt/hermit-codex/bin:"$PATH"']


def test_a_mounted_binarys_version_is_read_among_other_lines_of_its_exec(checks):
    """Harbor's docker exec merges stderr into stdout, and an image's BASH_ENV may echo: lines before and after what
    the mount says to --version (Codex's parse takes the first line, Pi's the last) do not reject a mount of the
    asked-for version, whose own lines are read between marks of their own."""
    r = report(checks, "installs")
    for name in ("codex_mounted_noisy", "pi_mounted_noisy"):
        assert r[name]["raised"] is None and r[name]["mounted"] is True, name


def test_a_musl_image_is_told_by_its_ldd_under_harbors_pipefail(checks):
    """musl's ldd says what it is on stderr and exits 1: under the pipefail Harbor's exec_as_root and exec_as_agent
    put first (dsh's install sets its own), an image that is musl without being Alpine is still found to be musl, so
    OpenCode's musl build is the one downloaded and dsh refuses the image before any download."""
    r = report(checks, "installs")
    assert r["musl_test"] == {"musl": "musl", "glibc": "glibc"}
    assert r["opencode_url_musl"].endswith("-musl.tar.gz") and not r["opencode_url_glibc"].endswith("-musl.tar.gz")
    assert r["dsh_musl"]["code"] == 1 and "this image is musl" in r["dsh_musl"]["stderr"]
    assert r["dsh_musl"]["curl"] == ""


def test_an_install_that_fails_is_a_launch_failure_not_the_agents_zero(checks):
    """What Harbor's setup runs before the agent phase (dsh refusing a musl image, Pi's npm install failing) is not the
    agent's doing: it raises HermitLaunchError naming the install, which Hermit's side counts as infra (launch_failed),
    not as the agent's 0 that a run would keep; Harbor's setup timeout cancelling the install stays its timeout."""
    r = report(checks, "install_failures")
    for name, cli, said in (("dsh_musl", "dsh", "this image is musl"), ("pi_npm", "pi", "No matching version found")):
        assert r[name] is not None and r[name][0] == "HermitLaunchError", (name, r[name])
        assert r[name][1].startswith(f"hermit: installing {cli} failed: NonZeroAgentExitCodeError: Command failed "
                                     "(exit 1): "), name
        assert said in r[name][1], name
        assert verdict(r[name]) == ("infra", "launch_failed"), name
    assert r["pi_setup_timeout"][0] == "TimeoutError"


def test_a_mounted_binary_that_did_not_serve_is_not_the_sessions_program_the_installed_cli_is(checks):
    """launch.render makes the mounted binary's path the command's program: when the mount is another version here
    (the class's install ran instead), the session runs the installed CLI found on PATH, not the mount; a mount that
    served stays the program."""
    r = report(checks, "mount_rejected")
    rejected, served = r["rejected"], r["served"]
    assert rejected["raised"] is None and rejected["mounted"] is False
    assert '  setsid codex exec -C /app "$HERMIT_PROMPT_TEXT" > /logs/agent/agent-codex.jsonl 2>&1 < /dev/null &' \
        in rejected["launch"].splitlines()
    assert "/opt/hermit-codex" not in rejected["launch"]
    assert served["raised"] is None and served["mounted"] is True
    assert ('  setsid /opt/hermit-codex/bin/codex exec -C /app "$HERMIT_PROMPT_TEXT" > /logs/agent/agent-codex.jsonl '
            '2>&1 < /dev/null &') in served["launch"].splitlines()
    assert 'export PATH=/opt/hermit-codex/bin:"$PATH"' in served["launch"].splitlines()


def test_a_mounted_binary_the_sessions_user_may_not_run_is_not_used_and_the_class_installs_the_cli(checks):
    """A mount of the asked-for version that only root may run (mode 700, a uid of this machine's), in a task whose
    agent user is not root (Harbor's default user for the install as for the session): its version, asked as root, is
    the asked-for one, but that user's `test -x` fails, so the mount is not used, the class's install runs and the
    session's program is the installed CLI found on PATH, where the mount would have made every session exit 126."""
    got = report(checks, "mount_rejected")["root_only"]
    assert got["asked"] == [["root", "version"], [None, "test -x"]]
    assert got["raised"] is None and got["mounted"] is False and got["installed"] is True
    assert '  setsid codex exec -C /app "$HERMIT_PROMPT_TEXT" > /logs/agent/agent-codex.jsonl 2>&1 < /dev/null &' \
        in got["launch"].splitlines()
    assert "/opt/hermit-codex" not in got["launch"]


def test_a_mounted_codex_is_used_though_it_warns_before_its_version_under_an_agent_users_unwritable_home(checks):
    """A mount of the asked-for version that the task's agent user may run, in a task whose agent user's HOME is not
    writable (HOME=/: a uid without a passwd entry, a home never created): Codex run by that user warns before its
    version, a line Harbor's parse_version would take for the version. Its version is asked as root, and only
    `test -x` as that user, so the mount is used, nothing is installed (Harbor's install would run under that HOME
    too) and the session's program is the mount."""
    got = report(checks, "mount_rejected")["home_unwritable"]
    assert got["asked"] == [["root", "version"], [None, "test -x"]]
    assert got["raised"] is None and got["mounted"] is True and got["installed"] is False
    assert ('  setsid /opt/hermit-codex/bin/codex exec -C /app "$HERMIT_PROMPT_TEXT" > /logs/agent/agent-codex.jsonl '
            '2>&1 < /dev/null &') in got["launch"].splitlines()


def test_the_launch_script_runs_the_codex_harbors_install_put_under_the_agent_users_nvm(checks):
    """Harbor's Codex install on a glibc image puts codex under the agent user's nvm, and none on the image's PATH:
    for real with bash, with that user's HOME and a container PATH without codex, the launch script of a plan that
    loads nvm (Codex's, as hermit.harbor.launch renders it) runs that codex, and it is the codex on the session's
    PATH."""
    r = report(checks, "codex_nvm")["installed"]
    [installed] = r["programs"]
    assert r["raised"] is None, r["raised"]
    assert r["stream"].splitlines() == [f"ran: {installed}", f"codex on PATH: {installed}"]


def test_a_mounted_codex_stays_the_sessions_program_and_first_on_its_path_with_nvm_loaded(checks):
    """For real with bash, a mounted codex that served, with the agent user's nvm loaded too (its codex installed
    there): the session runs the mount, and the mount's folder, put on PATH after nvm's, comes first on it."""
    r = report(checks, "codex_nvm")["mounted"]
    mount = r["programs"][1]                                                        # after nvm's own codex
    assert r["raised"] is None, r["raised"]
    assert r["stream"].splitlines() == [f"ran: {mount}", f"codex on PATH: {mount}"]


def test_mini_swe_agent_runs_hermits_runner_from_the_bundle_in_its_own_python_or_not_at_all(checks):
    """The runner's interpreter (a host path in the plan) becomes mini-swe-agent's uv tool Python, with the bundle
    first on PYTHONPATH; when the runner does not import there, the trial is a launch failure saying why (infra on
    Hermit's side), and no other agent runs in its place."""
    r = report(checks, "mini_swe_agent")
    bundle, no_import = r["bundle"], r["no_import"]
    assert bundle["raised"] is None
    assert (f"  setsid {TOOL_PYTHON} -P -m hermit.agent.base.mini_swe_agent_run --settings "
            "/installed-agent/hermit-home/mini-swe-agent.json --cwd /app --last "
            "/installed-agent/hermit-home/last_message.txt --stdin > /logs/agent/agent-mini-swe-agent.jsonl 2>&1 < "
            "/installed-agent/hermit/run/prompt.txt &") in bundle["launch"].splitlines()
    assert "export PYTHONPATH=/installed-agent/bundle:/app/lib" in bundle["launch"].splitlines()
    assert 'export PATH=/installed-agent/hermit/bin:"$PATH"' in bundle["launch"].splitlines()
    assert "/installed-agent/bundle" in bundle["bundle_upload"]
    assert bundle["probe"][0][1] == {"PYTHONPATH": "/installed-agent/bundle:/app/lib"}
    assert bundle["tool"].startswith(f"#!{TOOL_PYTHON}\n") and 'os.environ.get("HERMIT_TOOLS_URL"' in bundle["tool"]
    assert no_import["raised"][0] == "HermitLaunchError"
    assert no_import["raised"][1].startswith("hermit: Hermit's mini-swe-agent runner cannot run in this container: "
                                             f"hermit.agent.base.mini_swe_agent_run does not import in {TOOL_PYTHON}: "
                                             "ModuleNotFoundError")
    assert no_import["launch"] is None                                    # no session of anything else
    assert bundle["leaks"] == [] and no_import["leaks"] == []


def test_mini_swe_agents_python_is_found_among_other_lines_of_its_exec(checks):
    """Harbor's docker exec merges stderr into stdout: mini-swe-agent's Python is read from a marked line, so a line
    after it (a warning on stderr) does not keep Hermit's runner from running."""
    noisy = report(checks, "mini_swe_agent")["noisy"]
    assert noisy["raised"] is None
    assert f"  setsid {TOOL_PYTHON} -P -m hermit.agent.base.mini_swe_agent_run --settings " in noisy["launch"]


def test_the_hermit_tool_script_lists_and_calls_the_tool_servers_tools(checks):
    """The command an adapter's tools are for a CLI that only runs bash (mini-swe-agent), against a stand-in of
    Hermit's tool server: the list, a call, and the errors the model sees."""
    r = report(checks, "tool_script")
    assert r["list"] == [0, "lookup: Finds a row."]
    assert r["call"] == [0, "row 7"] and r["seen"] == [["/tok/call/lookup", {"id": 7}]]
    assert r["unknown"] == [1, "Error: ValueError: Unknown tool: nope"]
    assert r["bad_json"][0] == 1 and r["bad_json"][1].startswith("Error: JSONDecodeError")
    assert r["too_many"][0] == 1 and "as a command of its own" in r["too_many"][1]
    assert r["no_url"] == [1, "Error: no tools are available here"]


def test_the_hermit_tool_listing_says_error_when_the_tool_server_answers_one(checks):
    """GET <url>/tools answered with an HTTP error, in any shape but a list (a JSON object saying so by its "error" or
    by FastAPI's "detail", a JSON string, a number): the model reads `Error: ...` saying what was answered, as for any
    other error (exit 1), not a traceback from listing the answer as tools."""
    r = report(checks, "tool_script")
    assert r["list_error"] == {"gone": [1, "Error: RuntimeError: no such path", ""],
                               "detail": [1, "Error: RuntimeError: Not Found", ""],
                               "text": [1, "Error: RuntimeError: Bad Gateway", ""],
                               "number": [1, "Error: RuntimeError: 5", ""]}


def test_the_hermit_tool_listing_says_error_when_the_tool_servers_list_holds_no_tools(checks):
    """GET <url>/tools answered with a list whose items are not tools (not objects; an object without a name): the
    model reads `Error: ...` saying what was answered (exit 1), as for any other answer that is not the tools, not a
    traceback from listing it."""
    r = report(checks, "tool_script")
    assert r["list_not_tools"] == {"strings": [1, "Error: RuntimeError: ['lookup']", ""],
                                   "nameless": [1, "Error: RuntimeError: [{'description': 'Finds a row.'}]", ""]}


def test_the_answer_agent_asks_once_and_writes_the_formatted_answer_into_the_task(checks):
    """LAB-Bench's shape: the question is a file the task's entrypoint writes (waited for), a figure goes with it as
    an image data URL (a text file does not), the answer is the reply's last tagged match, written in the tagged
    form as the task's agent user; answer.json keeps no image bytes and no endpoint token. Harbor's environment has
    an http(s)_proxy that is not there: the endpoint on this machine is reached directly."""
    r = report(checks, "answer")
    assert r["raised"] is None and r["polls"] == 3
    path, body = r["request"]
    assert path == "/endpoint-token-1f2e3d/chat/completions"
    assert body["model"] == "kimi" and body["max_tokens"] == 256 and body["reasoning_effort"] == "high"
    assert body["messages"] == [
        {"role": "system", "content": "Context: tables are in /app/data."},
        {"role": "user", "content": [{"type": "text", "text": "Which panel shows the knockdown?\n"},
                                     {"type": "image_url", "image_url": {"url": r["png_url"]}}]}]
    assert r["written"] == "[ANSWER]C[/ANSWER]" and r["writes"][0].endswith("| base64 -d > /app/answer.txt")
    assert r["write_users"] == [None]                                                   # the task's agent user
    assert r["answer_json"] == {
        "reply": "First [ANSWER]A[/ANSWER], then on reflection [ANSWER] C [/ANSWER].", "answer": "C",
        "usage": {"prompt_tokens": 120, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 100}},
        "model": "kimi-served", "attachments": ["/app/fig.png"]}
    context = r["context"]
    assert [context["n_input_tokens"], context["n_output_tokens"], context["n_cache_tokens"]] == [120, 9, 100]
    assert context["metadata"] == {"plan_digest": "d1", "hermit_cli": "answer", "answer_file": "/app/answer.txt",
                                   "attachments": 1, "instruction_from": "file"}


def test_the_answer_agent_without_a_match_or_a_file_and_when_its_model_or_question_is_missing(checks):
    """No match: the whole reply is the answer; no answer file: nothing is written. A model that failed or was not
    reached raises NonZeroAgentExitCodeError (the verifier still runs), naming no endpoint token; an instruction
    file that never comes raises HermitLaunchError after wait_s; a bad answer block is refused at construction."""
    r = report(checks, "answer")
    assert r["plain_raised"] is None
    assert r["plain_request"]["messages"][1] == {"role": "user", "content": "What is 6 x 7?"}
    assert "max_tokens" not in r["plain_request"] and r["plain_answer"]["answer"] == "Just 42."
    assert r["plain_execs"] == []
    assert r["failed"][0] == r["error_class"]
    assert r["failed"][1].startswith("hermit: the answer's model failed: HTTP 500: ")
    assert "upstream exploded" in r["failed"][1]
    assert r["unreached"][0] == r["error_class"]
    assert r["unreached"][1].startswith("hermit: the answer's model was not reached: ")
    assert "endpoint-token" not in r["failed"][1] + r["unreached"][1]
    assert r["late"][0] == "HermitLaunchError" and "/app/instruction.md was not there after 0.2 s" in r["late"][1]
    assert r["capabilities"] is True and r["name"] == "hermit-answer"
    for field in ("endpoint", "model", "attachments", "answer_format", "answer_pattern", "max_tokens"):
        assert f"answer.{field}" in r["bad"], field


def test_the_answer_agent_reads_the_question_and_a_figure_among_other_lines_of_its_execs(checks):
    """Harbor's docker exec merges stderr into stdout, and an image's BASH_ENV may echo: the question, a figure's size
    and its bytes are each found on a marked line of their own, for real with bash; the answer is written."""
    r = report(checks, "answer")
    assert r["noisy_raised"] is None
    assert r["noisy_request"]["messages"][-1]["content"] == [
        {"type": "text", "text": "Which panel shows the knockdown?\n"},
        {"type": "image_url", "image_url": {"url": r["png_url"]}}]
    assert r["noisy_written"] == "B"


def test_the_answer_agent_in_an_image_without_base64_fails_to_launch_rather_than_ask_an_empty_question(checks):
    """For real with bash, no base64 on the container's PATH: without pipefail, `base64 < f | tr -d '\\n'` succeeds
    with nothing, so the question and a figure would be read as empty ones. They cannot be read: HermitLaunchError
    saying so, at once (not after wait_s), which Hermit's side counts as infra (launch_failed); the model is not
    asked."""
    r = report(checks, "answer")
    for name in ("no_base64_question", "no_base64_figure"):
        assert r[name] is not None and r[name][0] == "HermitLaunchError", (name, r[name])
        assert "no base64 or tr in the image" in r[name][1], name
        assert verdict(r[name]) == ("infra", "launch_failed"), name
    assert r["no_base64_question"][1].startswith("hermit: the task's instruction file /app/instruction.md could not "
                                                 "be read: ")
    assert r["no_base64_figure"][1].startswith("hermit: the attachment ")          # its path as ShellEnv maps it
    assert "/app/fig.png could not be read: hermit: no base64 or tr in the image" in r["no_base64_figure"][1]
    assert r["no_base64_asked"] == 0


def test_the_answer_agent_fails_to_launch_rather_than_ask_without_a_figure_it_cannot_measure_or_read(checks):
    """A figure the task names goes with the question, or the trial fails to launch (such a figure was left out with
    a warning, and the model asked without it): an image without wc (for real with bash, in the C locale: the
    figure's size cannot be measured) and, on FakeEnv's stand-ins, a read whose exec failed otherwise (compose's
    service had stopped), a measure that exits 0 without its hermit-size line or with one whose value is no number,
    and a read that exits 0 without its data line, are HermitLaunchError saying why, which Hermit's side counts as
    infra; the model is not asked."""
    r = report(checks, "answer")
    stand_ins = ("failed_read_figure", "unmarked_size_figure", "odd_size_figure", "unmarked_data_figure")
    for name in ("no_wc_figure", *stand_ins):
        raised = r[name]["raised"]
        assert raised is not None and raised[0] == "HermitLaunchError", (name, raised)
        assert verdict(raised) == ("infra", "launch_failed") and r[name]["asked"] == 0, name
    assert "/app/fig.png could not be measured: " in r["no_wc_figure"]["raised"][1]   # its path as ShellEnv maps it
    assert "wc: command not found" in r["no_wc_figure"]["raised"][1]
    assert {name: r[name]["raised"][1] for name in stand_ins} == {
        "failed_read_figure": 'hermit: the attachment /app/fig.png could not be read: service "main" is not running',
        "unmarked_size_figure": "hermit: the attachment /app/fig.png could not be measured: something else",
        "odd_size_figure": "hermit: the attachment /app/fig.png could not be measured: hermit-size:12 abc",
        "unmarked_data_figure": "hermit: the attachment /app/fig.png could not be read: something else"}


def test_the_answer_agent_fails_to_launch_rather_than_ask_without_any_figure_when_the_listing_exec_failed(checks):
    """The globs are listed in one exec whose command cannot fail (it ends in `true`): when that exec failed itself
    (compose's service had stopped, on FakeEnv's stand-in), which figures the task has is not known. HermitLaunchError
    saying so (infra), where the model was asked without any figure; the model is not asked."""
    got = report(checks, "answer")["failed_listing"]
    raised = got["raised"]
    assert raised == ["HermitLaunchError",
                      'hermit: the attachments /app/*.png could not be listed: service "main" is not running'], raised
    assert verdict(raised) == ("infra", "launch_failed") and got["asked"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a file of mode 000")
def test_the_answer_agent_fails_to_launch_rather_than_ask_without_a_figure_its_agent_user_may_not_open(checks):
    """For real with bash in the C locale, a figure of mode 000: the task's agent user cannot open it, so its size
    cannot be measured. HermitLaunchError saying so (infra), where the figure was left out with a warning and the
    model asked without it; the model is not asked."""
    got = report(checks, "answer")["locked_figure"]
    raised = got["raised"]
    assert raised is not None and raised[0] == "HermitLaunchError", raised
    assert "/app/fig.png could not be measured: " in raised[1] and "Permission denied" in raised[1]
    assert verdict(raised) == ("infra", "launch_failed") and got["asked"] == 0


def test_the_answer_agent_leaves_out_a_figure_over_the_size_limit_with_a_warning_and_asks_with_the_others(checks):
    """For real with bash, a figure a byte over MAX_ATTACHMENT_BYTES (each is sent inline) is left out with a
    warning, not a failure: the model is asked with the figure within the limit."""
    r = report(checks, "answer")
    assert r["over_limit_raised"] is None
    assert r["over_limit_content"] == [{"type": "text", "text": "What is 6 x 7?"},
                                       {"type": "image_url", "image_url": {"url": r["png_url"]}}]
    [warned] = r["over_limit_warned"]
    assert "/app/huge.png is " in warned and f"over {r['max_bytes']} bytes: it is not sent" in warned


def test_the_answer_agent_asks_once_with_the_plain_question_when_a_glob_matches_no_file(checks):
    """For real with bash, a glob of the attachments that matches no file (an /app that holds no figure) is no
    failure, where a listing whose exec failed is one: nothing goes with the question, and the model is asked once,
    with the plain question as the message's content."""
    r = report(checks, "answer")
    assert r["no_match_raised"] is None and r["no_match_asked"] == 1
    assert r["no_match_content"] == "What is 6 x 7?"


def test_the_answer_agents_answer_this_machine_could_not_write_or_keep_is_a_launch_failure_not_the_agents_zero(checks):
    """The model answered, and then the exec that writes the answer into the task could not be run here (its compose
    client not spawned: an OSError from the exec itself, EMFILE, EAGAIN on a loaded host), or answer.json could not be
    kept on this machine (its disk quota reached, the answer then not written): HermitLaunchError saying what failed,
    which Hermit's side counts as infra (launch_failed), as it does a write the container refused, not an unclassified
    agent error scored 0."""
    r = report(checks, "answer")
    for name, number in (("write_unspawned", errno.EMFILE), ("write_unspawned_eagain", errno.EAGAIN)):
        got, raised = r[name], OSError(number, os.strerror(number))      # as Python raises it: EAGAIN's BlockingIOError
        assert got["raised"] == ["HermitLaunchError", "hermit: the answer could not be written to /app/answer.txt: its "
                                 f"exec could not be run here: {type(raised).__name__}: {raised}"], (name, got)
        assert verdict(got["raised"]) == ("infra", "launch_failed") and got["kept"] is True, name
    refused = r["write_refused"]
    assert refused["raised"] == ["HermitLaunchError", "hermit: the answer could not be written to /app/answer.txt as "
                                 "the task's agent user: mkdir: cannot create directory '/app': Permission denied"]
    assert verdict(refused["raised"]) == ("infra", "launch_failed") and refused["kept"] is True
    unkept, raised = r["unkept"], OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))
    assert unkept["raised"] == ["HermitLaunchError", f"hermit: the answer could not be kept in {unkept['logs']}/"
                                f"answer.json: OSError: {raised}"], unkept
    assert verdict(unkept["raised"]) == ("infra", "launch_failed") and unkept["writes"] == []


def test_every_class_refuses_unknown_kwargs_a_bad_plan_and_harbors_model_overrides(checks):
    """The old deploy_dir is gone (the runner writes `plan`); a plan of another version or CLI, or with a path that
    escapes, is refused when the agent is made; Harbor's own model connection is off (no base_url, no credential
    check, nothing inferred) because the model is the plan's."""
    r = report(checks, "options")
    names = {"codex": "codex", "claude": "claude-code", "pi": "pi", "opencode": "opencode", "dsh": "dsh",
             "mini-swe-agent": "mini-swe-agent"}
    for cli, got in r["classes"].items():
        assert "Unknown option 'bogus'" in got["refused"]["bogus"], cli
        assert "Unknown option 'deploy_dir'" in got["refused"]["deploy_dir"], cli
        assert "is not a file" in got["refused"]["missing_plan"], cli
        assert got["name"] == names[cli] and got["import_path"] == ":".join(CLASSES[cli]), cli
        assert got["bin_option"] is (cli != "mini-swe-agent"), cli
    assert "is version 2 and this harborside reads version 1" in r["version"]
    assert "is for claude, and this agent runs codex" in r["cli"]
    assert "stream must be a file name in /logs/agent" in r["stream"]
    assert "home.secret_files must be paths relative to the home" in r["secret_path"]
    assert "env_from_host has a variable name that is not one" in r["env_name"]
    assert r["base_url"] == "Agent 'codex' does not support inference connection overrides"
    assert r["preflight"] == "passed" and r["connection"] == [None, None, None]
    assert r["no_plan"][0] == "HermitLaunchError" and r["no_plan"][1].endswith("(kwargs.plan), and it was given none")


def test_scripted_agent_runs_the_script(checks):
    r = report(checks, "scripted")
    assert r["uploads"] == [["/logs/agent/instruction.md", "Write hi."]]
    run = [e for e in r["execs"] if e[1].startswith("bash -c")]
    assert run and run[0][1] == "bash -c 'echo hi > /app/answer.txt'"
    assert run[0][2]["HERMIT_INSTRUCTION_FILE"] == "/logs/agent/instruction.md"
    assert r["exit_code"] == 3 and "[exit 3]" in r["script_txt"] and "script ran" in r["script_txt"]
    assert r["context"]["n_input_tokens"] == 0 and r["context"]["metadata"]["exit_code"] == 3
    assert r["name"] == "scripted"
    assert "bogus" in r["unknown"]


def test_driver_answers_requests_in_order(checks):
    r = report(checks, "driver_protocol")
    assert r["before_ready"]["ready"] is False and r["ready"]["ready"] is True and r["ready"]["next_index"] == 1
    assert r["out_of_order"] == {"ok": False, "out_of_order": True, "next_index": 1}
    assert r["busy"] == {"ok": False, "busy": "s1", "next_index": 1}
    assert r["stored"]["index"] == 1 and r["missing"]["ok"] is False
    assert r["queued"] == ["s1", {"ok": True, "index": 1, "step": {}}]
    assert r["errored"] == {"ok": False, "error": "SandboxBuildFailedError: no image"}
    assert r["stop_after_error"]["stopped"] is True
    assert r["finished"]["ok"] is False and "finished" in r["finished"]["error"]
    assert "unknown op" in r["unknown"]["error"]


def test_the_driver_gives_each_step_its_own_launch_plan(checks):
    """A stream keeps one agent: each step's plan (in the request, or in the agent kwargs the stream client sends)
    replaces the last one before the step runs; a step that gives none keeps it."""
    r = report(checks, "driver_plan")
    assert r["from_kwargs"] == ["codex", "exec", "second"]
    assert r["from_request"] == ["codex", "exec", "-C", "@@HERMIT_WORKDIR@@", "@@HERMIT_PROMPT@@"]
    assert r["kept"] == r["from_request"] and r["no_plan_agent"] == "ran"


def test_a_request_for_a_step_the_task_has_not_gets_an_error_reply_and_the_driver_goes_on(checks):
    """An unknown step name is answered (not left for the runner to wait out) and the loop serves the next request."""
    r = report(checks, "driver_loop")
    assert r["unknown"]["ok"] is False and "nope" in r["unknown"]["error"]
    assert r["next"]["ok"] is True and r["next"]["index"] == 1 and r["ran"] == ["s1"]


def test_a_steps_launch_plan_the_agent_cannot_take_is_a_launch_failure_of_the_step(checks):
    """A plan the agent refuses (another CLI's) fails the step as HermitLaunchError, which Hermit's side counts as
    infra (launch_failed), not as the agent's 0; the step is not run."""
    r = report(checks, "driver_loop")
    step = r["bad_plan"]["step"]
    assert step["exception_info"]["exception_type"] == "HermitLaunchError"
    assert "is for claude, and this agent runs codex" in step["exception_info"]["exception_message"]
    assert (errors.classify(step)["status"], errors.classify(step)["failure_cause"]) == ("infra", "launch_failed")
    assert r["ran"] == ["s1"] and r["loop_done"] is True and "loop_error" not in r


def test_the_driver_gives_the_streams_trial_the_runs_timeout_multipliers(checks):
    """The runner starts the driver with --trial-json: the verifier's and the environment build's timeout multipliers
    reach the stream's TrialConfig, as a job's trials get them; none given, Harbor's defaults; another key is refused,
    saying which ones it takes, before any trial."""
    r = report(checks, "driver_config")
    assert r["given"]["multipliers"] == [2.5, 3.0]
    assert r["default"]["multipliers"] == [None, None]
    unknown = r["unknown"]
    assert unknown["multipliers"] is None and unknown["code"] == 2
    assert "agent_timeout_multiplier" in unknown["log"]
    assert "verifier_timeout_multiplier, environment_build_timeout_multiplier" in unknown["log"]


def test_harborside_is_harbor_and_stdlib_only():
    """harborside runs in Harbor's venv: it imports harbor, its own modules and the standard library, nothing of
    Hermit's. pydantic is Harbor's API for an agent's options; certifi is an optional fallback (its ImportError is
    handled). Every module keeps its docstring."""
    import ast
    allowed = set(sys.stdlib_module_names) | {"harbor", "harborside", "pydantic", "certifi"}
    for module in sorted((HARBORSIDE_ROOT / "harborside").glob("*.py")):
        tree = ast.parse(module.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):                  # a relative import is harborside's own
                imported.add("harborside" if node.level else node.module.split(".")[0])
        assert imported <= allowed, (module.name, sorted(imported - allowed))
        assert ast.get_docstring(tree), module.name
