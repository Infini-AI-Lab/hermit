"""A Harbor trial's launch plan (`<job>/launch/plan.json`, schema 1): the acting CLI's home, command line,
environment and stream, rendered on Hermit's side by the CLI Base Agent Hermit's own sessions use
(hermit.agent.base.cli: `prepare_home`, `home_env`, `session_env`, `command`), and carried out in the task's
container by harborside/hermit.py, which imports nothing of Hermit.

    <job>/launch/
      plan.json        what harborside uploads and runs; every host path in it absolute
      home/            the CLI's home as the Base Agent renders it, uploaded to /installed-agent/hermit-home
      work/            the empty workspace a trial without an adapter is rendered against
      bundle/          mini-swe-agent: the Hermit modules its runner imports (hermit.agent.base.mini_swe_agent_run)
      return/          what the container hands back to this machine (a ChatGPT sign-in Codex refreshed)

The Base Agent renders against host paths (the home above, the staged deployment or work/ as the workspace); the plan
names container paths: the home is CONTAINER_HOME, the workspace `@@HERMIT_WORKDIR@@` (the task's working directory,
which harborside finds), in the command, the environment and the home's text files. A path of the job's directory
(where the home and the workspace are rendered) that would remain in them is refused (`check`), and so is a secret's
value anywhere but a secret file: a key reaches the container by its variable's name (`env_from_host`: the value is in
Harbor's process environment, never in an argument, a file or the plan), Claude Code's subscription token on a
descriptor (`env_by_fd`), the ChatGPT sign-in as a secret file of the home that comes back after the trial
(`return_files`). The prompt goes on stdin for every CLI: Harbor's instruction, then `prompt_suffix`, the memory note
(hermit.agent.acting.workspace's MEMORY_NOTE) when the deployed adapter keeps notes, which are copied back after the
session (`copy_back`); `prompt_slash_prefix` goes before a prompt that starts with `/`, as the Base Agent's
prompt_text puts it (Claude Code's line break: it would run the prompt as a command). After the trial the runner reads
`agent/<stream>` with the same Base Agent (`outcome`, `parse`, `adjust_usage`), from the files `collect` brings out
(on a ChatGPT login, Codex's session logs too: their reports of the plan's usage).

The answer-only agent (`cli: answer`) has no home and no command: its plan's `answer` block names the model endpoint
the runner serves on this machine, the adapter's context as the system text, and where the answer goes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from hermit.harbor.clis import CONTAINER_HOME, entry

PLAN_VERSION = 1
PLAN_FILE = "plan.json"
LAUNCH_DIR = "launch"
HOME_DIR = "home"                                        # under LAUNCH_DIR: the CLI's home as rendered
RETURN_DIR = "return"                                    # ... and what the container hands back (return_files)
WORKDIR = "@@HERMIT_WORKDIR@@"
PROMPT = "@@HERMIT_PROMPT@@"
BUNDLE_DIR = "/installed-agent/hermit-bundle"            # mini-swe-agent's Hermit modules in the container
KILL_GRACE_S = 10
LAST_MESSAGE = "last_message.txt"
# What mini-swe-agent's runner imports of Hermit (its own modules, mini-swe-agent's and the standard library besides);
# tests/core/test_harbor_launch.py checks that nothing else is imported
MINI_BUNDLE = ("hermit/__init__.py", "hermit/agent/__init__.py", "hermit/agent/base/__init__.py",
               "hermit/agent/base/mini_swe_agent.py", "hermit/agent/base/mini_swe_agent.yaml",
               "hermit/agent/base/mini_swe_agent_run.py", "hermit/agent/base/mini_swe_agent_env.py",
               "hermit/agent/model/__init__.py", "hermit/agent/model/backends.py", "hermit/agent/model/backends.yaml")
# Files of the home outcome() and adjust_usage() read after the session, brought out of the container: the last
# message the CLI writes itself (Codex's -o, mini-swe-agent's runner's --last), OpenCode's session database with its
# write-ahead log (OpenCode keeps it in WAL mode: a session cut off leaves its last rows in -wal; a file the session did
# not leave is not collected)
COLLECT = {"codex": [(LAST_MESSAGE, LAST_MESSAGE)], "mini-swe-agent": [(LAST_MESSAGE, LAST_MESSAGE)],
           "opencode": [("data/opencode/opencode.db", "opencode.db"),
                        ("data/opencode/opencode.db-wal", "opencode.db-wal"),
                        ("data/opencode/opencode.db-shm", "opencode.db-shm")]}
# ... and on a login, what says how much of the plan the session used: Codex's session logs (their rate-limit reports,
# which hermit.agent.base.login.usage_of reads as it does in a Hermit session's own home)
COLLECT_LOGIN = {"chatgpt": [("sessions", "sessions")]}
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
VAR_REF = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
TEXT_LIMIT = 4 << 20                                     # a home file larger than this is left as it is (no path in it)


# ---- the model and the Base Agent's settings ----------------------------------------------------------------------
def binding_of(params: dict):
    """The model binding of the runner's params, or None for a plain model name: the Binding spec the config's
    loading kept (`binding`, or `model` as a loaded spec), else a `<backend>/<model>` reference resolved here against
    the backends registry (a runner built without the config's loading)."""
    from hermit.agent.model.backends import Binding, as_binding, registry
    spec = params.get("binding")
    if isinstance(spec, dict) and spec.get("base_url") and spec.get("model"):
        return Binding.from_spec(spec)
    model = params.get("model")
    if isinstance(model, dict) and model.get("base_url") and model.get("model"):
        return Binding.from_spec(model)
    if isinstance(model, (str, dict)) and model:
        return as_binding(model, registry())
    return None


def bind(settings: dict, kind: str, binding) -> dict:
    """`bind_agent_params` for `kind` over one resolved binding: the model id, the provider settings where the CLI
    reads them (Codex's `args`, Claude Code's `env`, Pi's, OpenCode's and dsh's `provider`), the price, the spec."""
    from hermit.agent.model.backends import bind_agent_params
    ref = {"backend": binding.backend.name, "model": binding.model, "price": binding.price,
           "extra_body": dict(binding.extra_body)}
    return bind_agent_params({**settings, "model": ref}, kind, {binding.backend.name: binding.backend})


def settings_for(cli: str, *, model=None, binding=None, effort=None, web_search: bool = False,
                 login: str | None = None, extra: dict | None = None) -> dict:
    """The Base Agent's settings for a trial of `cli`: the table's, then the run's own (`acting_agent.settings`), the
    model bound for the CLI's dialect (or its plain name, on a login), the effort, web search set either way, the
    login, and the prompt on stdin."""
    row = entry(cli)
    settings = {**json.loads(json.dumps(row["settings"])), **dict(extra or {})}
    settings.update(web_search=bool(web_search), stdin=True)
    if binding is not None and row["backend"] is None:   # the answer agent: the model's id, priced; its spec served
        settings.update(model=binding.model, price=binding.price, binding=binding.spec())
    elif binding is not None:
        settings = bind(settings, row["backend"], binding)
    elif isinstance(model, dict):                        # a model a CLI reaches (the answer agent's): the session's
        settings["model"] = str(model.get("model") or "")
    elif model:
        settings["model"] = str(model)
    if effort is not None and effort != "":
        settings["effort"] = "off" if effort is False else str(effort)
    if login:
        settings["login"] = str(login)
    return settings


# ---- rendering ----------------------------------------------------------------------------------------------------
def render(cli: str, settings: dict, *, deploy: dict | None, tools_url: str | None, job: Path,
           login: str | None = None, tools_name: str | None = None, workdir: str | None = None,
           bin_path: str | None = None, path_prepend: list[str] | None = None, metadata: dict | None = None,
           login_root: Path | None = None, sign_in: bool = True) -> Path:
    """The launch plan of a trial of `cli` (a table key other than answer) under `<job>/launch/`; its path. `settings`
    are the Base Agent's (settings_for), `deploy` the staging record (hermit.harbor.staging.stage) or None,
    `tools_url` the MCP URL of the adapter's tools served for the trial (hermit.harbor.tools) or None, `tools_name`
    their server's name (the adapter id with `-` as `_`, as the context names them). `bin_path` and `path_prepend`:
    a mounted CLI's program and folder in the container. `sign_in` false renders without the sign-in's copy (a check,
    a digest)."""
    from hermit.agent.base.interface import make
    from hermit.agent.base.login import NO_TOKEN, TOKEN_ENV
    row = entry(cli)
    backend = row["backend"]
    if backend is None:
        raise ValueError(f"{cli} has no CLI to launch: its plan is render_answer's")
    login = login or settings.get("login")
    launch = Path(job) / LAUNCH_DIR
    shutil.rmtree(launch, ignore_errors=True)
    home = launch / HOME_DIR
    home.mkdir(parents=True)
    work = Path(deploy["deploy"]) if deploy else launch / "work"
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
    home = home.resolve()
    settings = dict(settings)
    settings["stdin"] = True
    if bin_path:
        settings["bin"] = bin_path
    elif backend == "mini-swe-agent":
        settings["bin"] = "python3"           # Hermit's runner on mini-swe-agent's interpreter (HermitMiniSweAgent)
    if tools_url:
        settings["tools_name"] = tools_name or "hermit_tools"
    if deploy:
        from hermit.agent.acting.workspace import _session_args
        settings["args"] = [*(settings.get("args") or []), *_session_args(backend, deploy, work)]
        if backend == "opencode":             # the only skills OpenCode may load: the deployment's
            settings["skills"] = list(deploy.get("skills") or [])
    agent = make(backend, settings)
    agent.check_settings()                    # what its run() refuses: a trial's plan is rendered without it
    if login == "claude" and not os.environ.get(TOKEN_ENV):
        raise ValueError(NO_TOKEN)
    agent.prepare_home(home, [], tools_url, settings.get("tools_name") or "hermit_tools")
    command = [str(a) for a in agent.command(PROMPT, work, home / LAST_MESSAGE, session=None, roles=(),
                                             tools_url=tools_url)]
    by_fd = dict(agent.by_fd())
    env, from_host = {}, {}
    for name, value in (settings.get("env") or {}).items():
        _env_entry(str(name), str(value), env, from_host)
    env.update({k: v for k, v in agent.session_env().items() if k not in by_fd})
    tools_env = agent.settings.get("tools_env")          # the Base Agent's own default too (mini-swe-agent's)
    if tools_url and tools_env:
        env[tools_env] = tools_url.removesuffix("/mcp")  # the tools' JSON entrance, for a CLI that calls them by shell
    env.update(agent.home_env(home))
    env.update(row["env"])
    binding = settings.get("binding") or {}
    key_env = _key_env(binding)
    if key_env and backend != "claude":       # the CLI reads the key from its own variable (Claude Code: env above)
        from_host.setdefault(key_env, key_env)
    secret_files, return_files = [], []
    if login == "chatgpt" and sign_in:
        from hermit.agent.base import login as codex_login
        codex_login.prepare(Path(login_root or codex_login.ROOT), home)
        secret_files.append(codex_login.FILE)
        (launch / RETURN_DIR).mkdir()
        return_files.append({"from": f"{CONTAINER_HOME}/{codex_login.FILE}",
                             "to": str((launch / RETURN_DIR / codex_login.FILE).resolve())})
    try:
        return _finish(cli, row, agent, settings, command, env, from_host, by_fd, home=home, work=work, launch=launch,
                       job=Path(job), deploy=deploy, tools_url=tools_url, workdir=workdir, path_prepend=path_prepend,
                       metadata=metadata, secret_files=secret_files, return_files=return_files, login=login)
    except BaseException:
        for name in secret_files:                        # a plan refused: no copy of the sign-in is left behind
            (home / name).unlink(missing_ok=True)
        raise


def _finish(cli, row, agent, settings, command, env, from_host, by_fd, *, home: Path, work: Path, launch: Path,
            job: Path, deploy, tools_url, workdir, path_prepend, metadata, secret_files, return_files,
            login: str | None = None) -> Path:
    """The rest of `render`, after the sign-in: the paths translated, the plan assembled, checked and written."""
    backend = row["backend"]
    places = {str(home): CONTAINER_HOME, str(work): WORKDIR}
    command = [_translate(a, places) for a in command]
    env = {k: _translate(str(v), places) for k, v in env.items()}
    _translate_home(home, places, skip=set(secret_files))
    bundle = None
    if backend == "mini-swe-agent":
        bundle = {"host_dir": str(_mini_bundle(launch / "bundle")), "container_dir": BUNDLE_DIR}
        env["PYTHONPATH"] = BUNDLE_DIR
    prompt_suffix, copy_back = "", []
    deploy_block = None
    if deploy:
        from hermit.agent.acting.workspace import MEMORY, MEMORY_NOTE
        folder = f"hermit/{deploy['adapter_id']}"
        if (work / folder / MEMORY).is_file():
            note = (Path(folder) / MEMORY).as_posix()
            prompt_suffix = "\n" + MEMORY_NOTE.format(path=note)
            copy_back.append((Path(folder) / MEMORY.parent).as_posix())
        ours = work / row["context_file"] if row["context_file"] else None
        text = ours.read_text(encoding="utf-8", errors="replace") if ours is not None and ours.is_file() else ""
        deploy_block = {"work_dir": str(work), "adapter_id": deploy["adapter_id"], "context_file": row["context_file"],
                        "context_limit": context_limit(cli, settings, text),
                        "context_escaped": row["context_escaped"] if row["context_limit"] else None,
                        "hooks": bool(deploy.get("hooks")), "copy_back": copy_back}
    for name in [k for k in from_host if k in env]:   # a variable the session's own settings set later wins
        del from_host[name]
    collect = [{"from": f"{CONTAINER_HOME}/{src}", "to": dst}
               for src, dst in [*COLLECT.get(backend, []), *COLLECT_LOGIN.get(login or "", [])]]
    # what the Base Agent puts before a prompt that starts with `/` (Claude Code would run it as one of its commands)
    slash = agent.prompt_text("/")
    plan = {"version": PLAN_VERSION, "cli": cli, "label": agent.label, "workdir": workdir, "deploy": deploy_block,
            "home": {"host_dir": str(home), "container_dir": CONTAINER_HOME, "secret_files": secret_files},
            "bundle": bundle, "command": command, "stdin": bool(agent.on_stdin()), "prompt_suffix": prompt_suffix,
            "prompt_slash_prefix": slash[:-1] if slash != "/" and slash.endswith("/") else None,
            "env": env, "env_from_host": from_host, "env_by_fd": by_fd, "nvm": bool(row["nvm"]),
            "path_prepend": list(path_prepend or []), "stream": f"agent-{backend}.jsonl", "collect": collect,
            "return_files": return_files,
            "tools_script": {"url_env": agent.settings["tools_env"]} if tools_url and agent.settings.get("tools_env")
            else None,
            "kill_grace_s": KILL_GRACE_S,
            "metadata": {"hermit_cli": cli, "backend": backend, **(metadata or {})}}
    check(plan, job=Path(job), secret_files=secret_files)
    path = launch / PLAN_FILE
    path.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def render_answer(settings: dict, *, endpoint: str, job: Path, system: str = "", options: dict | None = None,
                  metadata: dict | None = None) -> Path:
    """The answer-only agent's launch plan: no home and no command, the `answer` block (the endpoint on this
    machine that serves the model, the model's id, the adapter's context as the system text, the answer's options:
    instruction_file, wait_s, attachments, answer_file, answer_pattern, answer_format, max_tokens); its path. An
    answer_file of null is written nowhere (/app/answer.txt when none is given); a wait_s of null is the default
    30 s."""
    options = dict(options or {})
    unknown = set(options) - {"instruction_file", "wait_s", "attachments", "answer_file", "answer_pattern",
                              "answer_format", "max_tokens"}
    if unknown:
        raise ValueError(f"agent.params.answer takes instruction_file, wait_s, attachments, answer_file, "
                         f"answer_pattern, answer_format and max_tokens, not {sorted(unknown)}")
    if options.get("answer_pattern"):
        re.compile(str(options["answer_pattern"]))
    answer_format = str(options.get("answer_format") or "{answer}")
    if "{answer}" not in answer_format:
        raise ValueError("agent.params.answer.answer_format must hold {answer}")
    launch = Path(job) / LAUNCH_DIR
    shutil.rmtree(launch, ignore_errors=True)
    launch.mkdir(parents=True)
    effort = settings.get("effort")
    plan = {"version": PLAN_VERSION, "cli": "answer", "label": "answer", "workdir": None, "deploy": None,
            "home": None, "bundle": None, "command": None, "stdin": False, "prompt_suffix": "",
            "prompt_slash_prefix": None, "env": {},
            "env_from_host": {}, "env_by_fd": {}, "nvm": False, "path_prepend": [], "stream": "answer.json",
            "collect": [], "return_files": [], "tools_script": None, "kill_grace_s": KILL_GRACE_S,
            "metadata": {"hermit_cli": "answer", **(metadata or {})},
            "answer": {"endpoint": endpoint, "model": str(settings.get("model") or ""), "system": system or "",
                       "instruction_file": options.get("instruction_file"),
                       "wait_s": float(30 if options.get("wait_s") is None else options["wait_s"]),
                       "attachments": [str(a) for a in options.get("attachments") or []],
                       "answer_file": None if options.get("answer_file", "") is None else
                                      options.get("answer_file") or "/app/answer.txt",
                       "answer_pattern": options.get("answer_pattern"), "answer_format": answer_format,
                       "max_tokens": int(options["max_tokens"]) if options.get("max_tokens") else None,
                       "effort": None if effort in (None, "") else str(effort)}}
    check(plan, job=Path(job), secret_files=[])
    path = launch / PLAN_FILE
    path.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def context_limit(cli: str, settings: dict, text: str = "") -> int | None:
    """The bytes the deployment's context file may hold once merged with the image's own: the table's limit, less a
    byte for each `context_escaped` in `text` (Hermit's own context: dsh escapes each `</system-reminder>` by one more
    byte, as hermit.hosts.dsh counts it; the image's own file's are counted where it is merged, the plan's
    `deploy.context_escaped`), else Codex's own cap (project_doc_max_bytes, past which Codex would cut the file without
    a word)."""
    row = entry(cli)
    if row["context_limit"]:
        return int(row["context_limit"]) - (text.count(row["context_escaped"]) if row["context_escaped"] else 0)
    if row["backend"] == "codex" and settings.get("project_doc_max_bytes"):
        return int(settings["project_doc_max_bytes"])
    return None


def _key_env(binding: dict) -> str | None:
    if not binding:
        return None
    from hermit.agent.model.backends import Binding
    return Binding.from_spec(binding).key_env_if_set()


def _env_entry(name: str, value: str, env: dict, from_host: dict) -> None:
    """A setting's variable into the plan: `$NAME` alone passes that variable by name (its value never written);
    other `$NAME`s are expanded as a session's are, unless one names a secret, which is refused (it would be
    written out)."""
    whole = re.fullmatch(r"\$(?:\{(\w+)\}|(\w+))", value)
    if whole:
        from_host[name] = whole.group(1) or whole.group(2)
        return
    named = [a or b for a, b in VAR_REF.findall(value)]
    secret = [n for n in named if SECRET_NAME.search(n)]
    if secret:
        raise ValueError(f"the acting agent's env {name} holds ${secret[0]} inside other text: a secret reaches the "
                         f"container only whole, by its variable's name ({name}: ${secret[0]})")
    env[name] = os.path.expandvars(value) if named else value


def _translate(text: str, places: dict[str, str]) -> str:
    for host, container in sorted(places.items(), key=lambda kv: -len(kv[0])):   # the longest first
        text = text.replace(host, container)
    return text


def _translate_home(home: Path, places: dict[str, str], skip: set[str]) -> None:
    """The home's text files with host paths made container paths (a binary file holds none of them)."""
    for path in sorted(p for p in home.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(home).as_posix()
        if rel in skip or path.stat().st_size > TEXT_LIMIT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = _translate(text, places)
        if new != text:
            path.write_text(new, encoding="utf-8")


def _mini_bundle(target: Path) -> Path:
    """The Hermit modules mini-swe-agent's runner imports, copied under `target`."""
    from hermit.sandbox import FRAMEWORK
    for rel in MINI_BUNDLE:
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(FRAMEWORK) / rel, target / rel)
    return target.resolve()


# ---- what a plan must never hold ----------------------------------------------------------------------------------
def secret_values() -> dict[str, str]:
    """{variable: value} of this process's secrets a plan must not hold: every key-like variable's value
    (hermit.redact's rule) and Claude Code's subscription token."""
    from hermit.agent.base.login import TOKEN_ENV
    from hermit.redact import key_values
    out = {name: value.decode() for name, value in key_values().items()}
    if len(os.environ.get(TOKEN_ENV) or "") >= 8:
        out[TOKEN_ENV] = os.environ[TOKEN_ENV]
    return out


def check(plan: dict, *, job: Path, secret_files) -> None:
    """Refuse a plan that names a path of this machine's job directory where the container reads it (its command,
    its environment, its home's text files) or that holds a secret's value anywhere but a secret file."""
    secrets = secret_values()
    home = Path((plan.get("home") or {}).get("host_dir") or "")
    texts = [("the command", " ".join(plan.get("command") or [])),
             ("the environment", json.dumps(plan.get("env") or {})),
             ("the answer block", json.dumps(plan.get("answer") or {})),
             ("the prompt suffix", plan.get("prompt_suffix") or "")]
    if plan.get("home"):
        for path in sorted(p for p in home.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(home).as_posix()
            if rel in set(secret_files) or path.stat().st_size > TEXT_LIMIT:
                continue
            try:
                texts.append((f"the home's {rel}", path.read_text(encoding="utf-8")))
            except (UnicodeDecodeError, OSError):
                continue
    job_text = str(Path(job).resolve())
    for where, text in texts:
        for name, value in secrets.items():
            if value and value in text:
                raise RuntimeError(f"hermit: the launch plan would hold the value of {name} in {where}: a secret "
                                   "reaches the container by its variable's name only")
        if job_text in text and where != "the answer block":
            raise RuntimeError(f"hermit: the launch plan names a path of this machine in {where} ({job_text}): the "
                               "container would not find it")


def digest(plan_path: Path) -> str:
    """A plan's identity for the trial cache: its command, environment, settings and home files (their paths and
    bytes, the secret files by name only), with what differs between two renders of the same launch taken out: the
    job's paths, the tools' URL (a token of each trial's server) and the answer endpoint's."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    job = str(Path(plan_path).resolve().parent.parent)
    url = re.compile(r"https?://[\w.:-]+/[0-9a-f]{32}")

    def plain(text: str) -> str:
        return url.sub("<url>", text.replace(job, "<job>"))
    h = hashlib.sha256()
    same = {k: v for k, v in plan.items() if k not in ("home", "bundle", "return_files", "deploy", "metadata")}
    if plan.get("deploy"):
        same["deploy"] = {k: v for k, v in plan["deploy"].items() if k != "work_dir"}
    h.update(plain(json.dumps(same, sort_keys=True)).encode())
    home = plan.get("home") or {}
    if home.get("host_dir"):
        root = Path(home["host_dir"])
        skip = set(home.get("secret_files") or [])
        for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(root).as_posix()
            h.update(b"\0" + rel.encode() + b"\0")
            if rel not in skip:
                data = path.read_bytes()
                try:
                    data = plain(data.decode("utf-8")).encode()
                except UnicodeDecodeError:
                    pass
                h.update(hashlib.sha256(data).digest())
    if plan.get("bundle"):
        root = Path(plan["bundle"]["host_dir"])
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            h.update(path.relative_to(root).as_posix().encode() + hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()
