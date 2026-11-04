"""Template check for `ahe` adapters: the tree is the AHE harness H (arXiv 2604.25850 §3.1) in the layout the hosts
read from the workspace's root, nothing lies off that surface, the seed system prompt survives (the paper's
non-deletable seed, §3.3), the memory channel is intact, the middleware is a module the adapter's hook runner can load
and call on every host, and ADAPTER.md is AGENTS.md (the framework's context export is the always-on text).

The rules on the files are stated here with the stdlib and the framework's YAML frontmatter reader
(hermit.spec._frontmatter). The rules on what reaches the agent run it, on every host the template supports (HOSTS,
the hosts of hook_hosts.py): a copy of the adapter is deployed by the framework's own deployment for the host
(hermit.hosts.codex.prepare, hermit.hosts.claude_code.prepare: what `hermit deploy` and the framework's agents run;
the framework is importable wherever check_spec runs this), its context file is measured against the host's limit, and
one session of the host's events (hook_hosts: `session`, payloads in the host's own shape and order) is run through
the hooks the deployment installed, as the host runs them: the deployed command, through a shell, the state carried
from event to event, every output held to the fields the host reads and to the drop hook_hosts knows of (a context
Codex drops when it comes with `allow`).

The surface:
    AGENTS.md                              system prompt + tool guidance: never empty, within what each host reads once
                                           deployed (Codex: 32,768 bytes; the deployment adds its managed block, a
                                           note on the adapter's folder, the list of the adapter's other files and two
                                           bytes per nested heading), the three seed rules present (whitespace-normalised)
    MEMORY.md                              long-term memory: present, starts with `# Long-term memory`
    middleware.py                          the hook pipeline: compiles; defines the five hooks (session_start,
                                           before_tool, after_tool, stop, before_model; the runner calls all five),
                                           each `(inp, state)`; other top-level functions are private helpers
                                           (`_name`); imports stdlib or hermit_hooks only; none of the network/process
                                           modules, by import or by call; session_start (or a helper it calls) reads
                                           MEMORY.md (the memory channel is never removed); every hook runs on every
                                           host through the deployed hooks without an error; and session_start, run on
                                           a MEMORY.md holding an entry, hands that entry to the model on every host
    scripts/<name>.py                      a tool implementation; must compile
    .agents/skills/<name>/SKILL.md         frontmatter `description:` required; `name:` optional, must equal the dir
    .agents/skills/<name>/scripts/<file>   a helper carried with its skill; a .py must compile
    agents/<name>.md                       a sub-agent (a role every host installs): frontmatter `name:` (= the file
                                           stem) and `description:` required; the stem is a role name on every host
                                           (a letter first, at most 64 characters of [a-z0-9_-]), unique when `-` and
                                           `_` are the same (Codex spells a role with `_`, Claude Code with `-`)
A script AGENTS.md or a SKILL.md invokes must be in the tree, named as a deployment lets the agent run it: relative
to the adapter's folder (`python3 scripts/<x>.py`), or through that folder (`python3 hermit/ahe/scripts/<x>.py`).
The fixed files (hooks.json, hook_runner.py, hermit_hooks.py, hook_hosts.py) and the placeholders of the on-demand
directories (scripts/README.md, .agents/skills/README.md, agents/README.md) are not part of the tree.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ALWAYS_ON = "AGENTS.md"
ALWAYS_ON_MAX_BYTES = 32_000          # the template's own cap; a host's limit on what it reads deployed may be lower
FOLDER = "hermit/ahe"                 # where a deployment puts the adapter's copy in the workspace (hermit/<id>/)
MEMORY = "MEMORY.md"
MEMORY_HEADER = "# Long-term memory"
MIDDLEWARE = "middleware.py"
RUNNER = "hook_runner.py"
INTERFACE = ("hermit_hooks.py", "hook_hosts.py")    # the runner's siblings: the interface and the hosts' wires
HOOKS = ("session_start", "before_tool", "after_tool", "stop", "before_model")
# `hermit_hooks` is the interface beside the runner. `hermit.runtime.hooks`, the framework module it was before, is
# still accepted: adapters trained then import it as a fallback inside a try (a dead branch under this runner).
HOOK_MODULES = frozenset({"hermit.runtime.hooks", "hermit_hooks"})
# A middleware runs inside every tool call of a trial: no network, no process spawning, no event loop, and no way
# around the import rule (importlib, ctypes). The sandbox already denies the network on the learner side; where the
# agent runs the rule is this check, static: the runner's own guards are its alarm and fail-open.
FORBIDDEN_MODULES = frozenset({"socket", "http", "urllib", "ssl", "ftplib", "smtplib", "xmlrpc", "asyncio", "subprocess",
                               "ctypes", "multiprocessing", "pty", "importlib", "posix", "nt", "_posixsubprocess"})
FORBIDDEN_CALLS = frozenset({"__import__", "builtins.__import__", "importlib.import_module", "os.system", "os.popen",
                             "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp"})
STAR_FORBIDDEN = frozenset({"os", "builtins"})   # `from os import *` would bind os.system & co. under their bare names
FORBIDDEN_CALL_PREFIXES = ("os.exec", "os.spawn")
ROOT = Path(__file__).resolve().parents[1]      # the template root this check is loaded from (its fixed files)
# The hosts the template runs on, each with the framework's deployment for it (hermit.hosts.<module>.prepare) and the
# setting of that module that bounds the context file the host reads (None: the host reads it whole).
DEPLOYMENTS = {"codex": ("hermit.hosts.codex", "DOC_BYTES", "Codex's project_doc_max_bytes"),
               "claude-code": ("hermit.hosts.claude_code", None, None)}
DRY_RUN_TIMEOUT_S = 40                # one hook's dry run: the runner's own alarm (28 s) fires first
DRY_RUN_BUDGET_S = 75                 # every dry run of the check together: inside check_spec's 120 s for the check
# The memory run: one entry in a copy of the adapter's MEMORY.md, carrying a mark found nowhere else, and a task on
# the entry's topic without the mark. session_start must hand the mark to the model: only the memory holds it, so a
# hook that echoes the task fails, and one that hands over the entries relevant to the task passes.
MEMORY_PROBE_MARK = "ahe-memory-probe-3f9c1a"
MEMORY_PROBE_ENTRY = f"When a table reports a value, check its units before answering. [{MEMORY_PROBE_MARK}]"
MEMORY_PROBE_TASK = "Read the table that reports the value, and check its units before answering."
# The paper's seed system prompt (Appendix B.1; $AHE/agents/code_agent_simple/systemprompt.md), the three rules, as
# the template's AGENTS.md carries them: the Evolve Agent may add to the prompt, never delete or reword these.
SEED_RULES = (
    "Prefer short replies; use the tool for actions.",
    "Before commands that delete or overwrite important data, state briefly what they do.",
    "Long-running processes: run them in the background and poll their output instead of blocking.",
)
# The framework's and the template's own files, and the placeholders: not part of the harness tree.
OWN = frozenset({"ADAPTER.md", "exports.yaml", "README.md", "hooks.json", RUNNER, *INTERFACE})   # and .hermit/
STUBS = frozenset({"scripts/README.md", ".agents/skills/README.md", "agents/README.md"})
NAME = r"[a-z0-9][a-z0-9_\-]*"
# A role is installed under its name on every host: Codex spells it with `_` for `-`, Claude Code with `-` for `_`
# (hermit.hosts.codex, hermit.hosts.inputs.subagent_name): a letter first, at most 64 characters.
AGENT_NAME = re.compile(r"[a-z][a-z0-9_\-]{0,63}")
RX = {"skill": re.compile(rf"\.agents/skills/({NAME})/SKILL\.md"),
      "skill_script": re.compile(rf"\.agents/skills/{NAME}/scripts/[A-Za-z0-9_.\-]+"),
      "agent": re.compile(rf"agents/({NAME})\.md"),
      "script": re.compile(r"scripts/[A-Za-z0-9_.\-]+\.py"),       # the manifest's learnable glob, scripts/*.py
      # `python3 scripts/x.py`, `python3 ./scripts/x.py`, `python3 hermit/ahe/scripts/x.py`,
      # `python3 .agents/skills/<n>/scripts/x.py`: the file must exist (the capture is the adapter-relative path).
      "script_ref": re.compile(rf"(?:python3?\s+)(?:\./)?(?:{re.escape(FOLDER)}/)?"
                               rf"((?:\.agents/skills/[\w\-]+/)?scripts/[\w.\-]+\.py)"),
      # Any mention of a skill helper, with or without an interpreter: the file must exist.
      "skill_file_ref": re.compile(r"(\.agents/skills/[\w\-]+/scripts/[\w\-][\w.\-]*)"),
      # The task container's /adapter of an earlier deployment: no deployment has it now.
      "adapter_ref": re.compile(r"/adapter/((?:\.agents/skills/[\w\-]+/)?scripts/[\w.\-]+)")}
# A frontmatter block closes on a whole `---` line, as Codex's skill loader requires (hermit.spec._frontmatter alone
# would also close it on `---oops`, and parse only the lines before it).
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)


def _hosts():
    """hook_hosts.py of the template root (the fixed one: check 1 holds the adapter's copy to it): the hosts' wires."""
    name = "hermit_hooks"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "hermit_hooks.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location("ahe_hook_hosts", ROOT / "hook_hosts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WIRES = _hosts()
HOST_WIRES = _WIRES.HOSTS             # host name -> its wire (events, sample payloads, what it reads)
HOSTS = _WIRES.HOOK_HOSTS             # the hosts that run the hooks themselves, in the order the checks run in;
                                      # DEPLOYMENTS names the deployment of each (Pi runs none: nothing to run)


def allowed(rel: str) -> bool:
    return rel in (ALWAYS_ON, MEMORY, MIDDLEWARE) or any(RX[k].fullmatch(rel)
                                                        for k in ("skill", "skill_script", "agent", "script"))


def read_tree(adapter: Path) -> tuple[dict[str, str], list[str]]:
    """rel -> text for every file on the surface, and the files that are off it."""
    tree, off = {}, []
    for p in sorted(adapter.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(adapter).as_posix()
        if rel in OWN or rel in STUBS or rel == ".hermit" or rel.startswith(".hermit/"):
            continue
        if allowed(rel):
            tree[rel] = p.read_text(encoding="utf-8", errors="replace")
        else:
            off.append(rel)
    return tree, off


def normalise(text: str) -> str:
    return " ".join(text.split())


def frontmatter(text: str) -> dict | None:
    """A leading frontmatter block parsed as YAML, as the framework reads it (hermit.spec._frontmatter; a sub-agent's
    deployment and the hosts' own skill loaders read YAML too), its values with their YAML types; None when the file
    has no block (FRONTMATTER: closed by a whole `---` line) or the block is not a YAML mapping."""
    from hermit.spec import _frontmatter
    m = FRONTMATTER.match(text)
    if m is None or re.search(r"^---", m.group(1), re.M):
        return None                 # a `---oops` line inside: the host parses the whole block, which is not YAML
    return _frontmatter(m.group(0))


def _text_field(head: dict, key: str) -> str | None:
    """head[key] when it is a non-empty YAML string, else None: a host that reads the field as a string refuses a
    list or a number (Codex: `invalid type: sequence, expected a string`) and skips the file."""
    value = head.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _not_text(head: dict, key: str) -> str:
    """Why head[key] is not a usable string, for a message: missing, empty, or the YAML type it has instead."""
    value = head.get(key)
    if key not in head or value is None or (isinstance(value, str) and not value.strip()):
        return "missing or empty"
    return f"a YAML {type(value).__name__}, not a string"


class Deployment:
    """A copy of the adapter deployed for one host into a scratch workspace by the framework's own deployment, with
    its context file read and its hook commands: what the agent of that host gets. `why` names a deployment that
    failed (and `named` the adapter file the failure names, found by check_spec's own rule for a deploy failure,
    hermit.spec._path_in, so that repair reverts that file alone)."""

    def __init__(self, host: str, workspace: Path):
        self.host, self.workspace = host, workspace
        self.result, self.why, self.named = None, "", ""
        self.limit, self.setting, self.context_bytes = None, None, None

    @property
    def copy(self) -> Path:
        return Path(self.result["adapter_dir"])

    def command(self, event: str) -> str | None:
        """The command the deployment installed for `event` in the host's own hook file, or None."""
        path = self.result.get("hooks") if self.result else None
        try:
            hooks = json.loads(Path(path).read_text(encoding="utf-8"))["hooks"] if path else {}
        except (OSError, ValueError, KeyError, TypeError):
            return None
        for group in hooks.get(event) or []:
            for hook in group.get("hooks") or []:
                if hook.get("type") == "command" and isinstance(hook.get("command"), str):
                    return hook["command"]
        return None


FIXED_RUNTIME = ("exports.yaml", "hooks.json", RUNNER, *INTERFACE)   # what deploys and runs a hook: the template
                                                                      # root's, as check 1 holds them


def deploy(adapter: Path, prompt: str, host: str, scratch: Path, memory: str | None = None) -> Deployment:
    """`adapter` with `prompt` as its AGENTS.md (and so as ADAPTER.md, which the Evolver keeps equal to it), and
    `memory` as its MEMORY.md when given, deployed for `host` into `scratch/<host>/workspace` by the framework's
    deployment, with no limit on the context: the result is measured against the host's limit here. The fixed files
    that run a hook are the template root's (check 1 holds the adapter's to them), so that a dry run's failure is the
    middleware's."""
    module, setting, label = DEPLOYMENTS[host]
    done = Deployment(host, scratch / host / "workspace")
    copy = scratch / host / "adapter"
    try:
        deployment = importlib.import_module(module)
        from hermit.spec import _path_in
    except ImportError as exc:
        done.why = f"the framework's deployment cannot be imported here ({type(exc).__name__}: {exc})"
        return done
    shutil.copytree(adapter, copy, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".hermit-hooks"))
    for name in FIXED_RUNTIME:
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, copy / name)
    for name in (ALWAYS_ON, "ADAPTER.md"):
        (copy / name).write_text(prompt, encoding="utf-8")
    if memory is not None:
        (copy / MEMORY).write_text(memory, encoding="utf-8")
    kwargs = {"context_limit": None} if setting else {}
    try:
        done.result = deployment.prepare(copy, done.workspace, sys.executable, **kwargs)
    except Exception as exc:                      # noqa: BLE001 - the deployment's own refusal, named
        why = f"{type(exc).__name__}: {exc}".replace(str(copy) + "/", "")
        done.why, done.named = (why if len(why) <= 600 else "..." + why[-600:]), _path_in(adapter, why)
        return done
    done.limit, done.setting = (getattr(deployment, setting), label) if setting else (None, None)
    context = done.workspace / done.result["context_injected_via"]
    done.context_bytes = len(context.read_bytes()) if context.is_file() else 0
    return done


def deploy_all(adapter: Path, prompt: str, scratch: Path) -> list[Deployment]:
    return [deploy(adapter, prompt, host, scratch) for host in HOSTS]


def prompt_problems(tree: dict[str, str], deployments: list[Deployment]) -> list[tuple[str, str]]:
    """(2) AGENTS.md: present, non-empty, within what every host reads once deployed, the seed rules intact."""
    body = tree.get(ALWAYS_ON, "")
    errs = []
    if not body.strip():
        return [(ALWAYS_ON, f"{ALWAYS_ON} is empty: it is the system prompt, the one always-on file, so an empty one "
                            f"is a harness that does not exist")]
    size = len(body.encode("utf-8"))
    for done in deployments:
        label = HOST_WIRES[done.host].label
        if done.result is None:
            errs.append((done.named, f"the adapter does not deploy to {label}, so {ALWAYS_ON} cannot be measured as "
                                     f"the agent reads it: {done.why}"))
            continue
        context = done.result["context_injected_via"]
        overhead = done.context_bytes - size
        cap = ALWAYS_ON_MAX_BYTES if done.limit is None else min(ALWAYS_ON_MAX_BYTES, done.limit - overhead)
        if size > cap:
            limit = (f"and a deployment past the {done.limit} bytes {label} reads ({done.setting}) is refused"
                     if done.limit is not None else f"over the template's {ALWAYS_ON_MAX_BYTES}-byte cap")
            errs.append((ALWAYS_ON, f"{ALWAYS_ON} is {size} bytes, over the {cap}-byte cap on {label}: the deployment "
                                    f"adds {overhead} bytes around it in {context} (its managed block, the note on the "
                                    f"adapter's folder, the list of the adapter's other files and two bytes per "
                                    f"nested heading), making {done.context_bytes} bytes, {limit}; it is injected "
                                    f"whole into EVERY turn"))
    flat = normalise(body)
    for rule in SEED_RULES:
        if normalise(rule) not in flat:
            errs.append((ALWAYS_ON, f"{ALWAYS_ON} no longer contains the seed rule \"{rule}\": the three seed rules are "
                                    f"the paper's non-deletable system prompt; add to the prompt, never delete or "
                                    f"reword them"))
    return errs


def memory_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """(4) MEMORY.md: present, with its header (the seed middleware reads below it)."""
    if MEMORY not in tree:
        return [(MEMORY, f"{MEMORY} is missing: the long-term memory is part of the harness even when it holds no "
                         f"entry yet; restore `{MEMORY_HEADER}` with an `## Agent Added Memories` section")]
    if not tree[MEMORY].lstrip().startswith(MEMORY_HEADER):
        return [(MEMORY, f"{MEMORY} must start with `{MEMORY_HEADER}`: the middleware hands the text below that "
                         f"header to the model at session start")]
    return []


def _parameters(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    """The number of plain parameters, or None when the signature takes *args / **kwargs."""
    a = fn.args
    if a.vararg is not None or a.kwarg is not None:
        return None
    return len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)


def _call_name(node: ast.Call) -> str:
    """`os.system(...)` -> "os.system", `__import__(...)` -> "__import__"; "" for anything else."""
    parts = []
    target = node.func
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if not isinstance(target, ast.Name):
        return ""
    parts.append(target.id)
    return ".".join(reversed(parts))


def _aliases(module: ast.Module) -> dict[str, set[str]]:
    """The names imports bind anywhere in the module (any function's scope), to everything they name there: `import
    os as o` binds o -> os, `from os import system as s` binds s -> os.system, so that a call through an alias is
    the call it makes, whichever binding is in force where it is called."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                out.setdefault(name, set()).add(alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                out.setdefault(alias.asname or alias.name, set()).add(f"{node.module}.{alias.name}")
    return out


def _resolve(called: str, aliases: dict[str, set[str]]) -> set[str]:
    """The call's name as written and as every import of its first part makes it."""
    head, dot, rest = called.partition(".")
    return {called} | {target + dot + rest for target in aliases.get(head, ())}


def _forbidden(names: set[str]) -> str | None:
    return next((n for n in sorted(names) if n in FORBIDDEN_CALLS or n.startswith(FORBIDDEN_CALL_PREFIXES)), None)


def middleware_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """(5) middleware.py: compiles, is shaped as the hook interface, imports nothing the runner cannot provide where
    the agent runs (memory_run_problems sees that it keeps the memory channel)."""
    if MIDDLEWARE not in tree:
        return [(MIDDLEWARE, f"{MIDDLEWARE} is missing: the hook runner calls it on every event, so the harness has no "
                             f"middleware and no memory channel; restore it (five hooks returning None are a valid, "
                             f"inert module)")]
    src = tree[MIDDLEWARE]
    try:
        module = ast.parse(src, MIDDLEWARE)
    except (SyntaxError, ValueError) as exc:
        detail = (f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError)
                  else f"{type(exc).__name__}: {exc}")
        return [(MIDDLEWARE, f"{MIDDLEWARE} does not compile: {detail[:160]}; the host's hooks then fail open on "
                             f"every event, silently")]
    errs = []
    defined = {n.name for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for hook in HOOKS:
        if hook not in defined:
            errs.append((MIDDLEWARE, f"{MIDDLEWARE} defines no `{hook}`: the hook runner calls all five hooks on the "
                                     f"events hooks.json registers; an unused hook is "
                                     f"`def {hook}(inp, state): return None`"))
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in HOOKS:
            if isinstance(node, ast.AsyncFunctionDef):
                errs.append((MIDDLEWARE, f"{MIDDLEWARE}: hook `{node.name}` is `async def`; the runner calls hooks "
                                         f"synchronously, so make it a plain `def`"))
            n = _parameters(node)
            if n != 2:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE}: hook `{node.name}` takes {n if n is not None else 'variable'} "
                                         f"parameters; every hook is `def {node.name}(inp, state)`: the HookInput "
                                         f"and the session's state dict, nothing else"))
        elif not node.name.startswith("_"):
            errs.append((MIDDLEWARE, f"{MIDDLEWARE}: top-level function `{node.name}` is not a hook; the runner calls "
                                     f"only {', '.join(HOOKS)}, so it would never run. A hook misspelled is dead "
                                     f"weight; a helper is private: name it `_{node.name}`"))
    stdlib = set(sys.stdlib_module_names)
    aliases = _aliases(module)
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            called = _forbidden(_resolve(_call_name(node), aliases))
            if called is not None:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE} line {node.lineno} calls `{called}`: a middleware runs inside "
                                         f"every tool call with no network and no process of its own, and the import "
                                         f"rule cannot be worked around; none of {', '.join(sorted(FORBIDDEN_CALLS))}, "
                                         f"os.exec*, os.spawn*"))
            continue
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE} line {node.lineno}: relative import; the runner loads the module "
                                         f"alone (not as a package), so import only the stdlib or hermit_hooks"))
                continue
            names = [node.module or ""]
            if any(alias.name == "*" for alias in node.names) and (node.module or "").split(".")[0] in STAR_FORBIDDEN:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE} line {node.lineno}: `from {node.module} import *` binds its "
                                         f"process and import calls under bare names the rule cannot follow; import "
                                         f"the names you use"))
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in FORBIDDEN_MODULES:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE} line {node.lineno} imports `{name}`: a middleware runs inside "
                                         f"every tool call with no network and no process of its own; none of "
                                         f"{', '.join(sorted(FORBIDDEN_MODULES))}"))
            elif name not in HOOK_MODULES and top not in stdlib:
                errs.append((MIDDLEWARE, f"{MIDDLEWARE} line {node.lineno} imports `{name}`, which is not where the "
                                         f"agent runs: only the stdlib and hermit_hooks, the interface beside the "
                                         f"runner, are importable there"))
    return errs


class Clock:
    """What remains of DRY_RUN_BUDGET_S for the dry runs of this check."""

    def __init__(self, budget: float | None = None):
        self.end = time.monotonic() + (DRY_RUN_BUDGET_S if budget is None else budget)

    def left(self) -> float:
        return self.end - time.monotonic()


def _python_on_path(scratch: Path) -> dict:
    """The environment a deployed hook runs in here: `python3` is this interpreter (the hook commands name
    `python3`), no log base of the caller's, nothing of a host's own."""
    bin_dir = scratch / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "python3"
    wrapper.write_text(f"#!/bin/sh\nexec {json.dumps(sys.executable)} \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("HERMIT_HOOK_LOGS", "CLAUDE_PROJECT_DIR")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _record(line: str) -> dict:
    """An audit line as a dict; {} for one that is not a JSON object."""
    try:
        record = json.loads(line)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def run_hook(done: Deployment, event: str, payload: dict, env: dict, clock: Clock):
    """The deployed command for `event` run as the host runs it (a shell, the event's JSON on stdin, from the
    session's workspace; Claude Code also sets $CLAUDE_PROJECT_DIR): (output, audit lines it added, problem)."""
    command = done.command(event)
    if command is None:
        return None, [], f"the deployment for {HOST_WIRES[done.host].label} installed no {event} hook"
    audit = done.workspace / ".hermit-hooks" / "hook-calls.jsonl"
    before = len(audit.read_text(encoding="utf-8").splitlines()) if audit.is_file() else 0
    run_env = dict(env, **({"CLAUDE_PROJECT_DIR": str(done.workspace)} if done.host == "claude-code" else {}))
    timeout = min(DRY_RUN_TIMEOUT_S, clock.left())
    if timeout <= 0:
        return None, [], "budget"
    try:
        proc = subprocess.run(["sh", "-c", command], cwd=str(done.workspace), env=run_env, input=json.dumps(payload),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, [], ("budget" if timeout < DRY_RUN_TIMEOUT_S else
                          f"it ran past {DRY_RUN_TIMEOUT_S} s although the runner stops a hook after 28 s")
    lines = audit.read_text(encoding="utf-8").splitlines()[before:] if audit.is_file() else []
    records = [_record(line) for line in lines]
    if proc.returncode != 0 or not records or records[0].get("event") != event:
        return None, records, (f"the hook exited {proc.returncode} without its audit line: "
                               f"{proc.stderr.strip()[-300:]}")
    failed = next((r for r in records if r.get("error")), None)
    if failed is not None:
        return None, records, failed
    if not proc.stdout.strip():
        return None, records, None
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return None, records, f"the hook's output is not one JSON document: {proc.stdout.strip()[:200]!r}"
    wrong = HOST_WIRES[done.host].output_problems(event, out)
    return out, records, (Unread("; ".join(wrong)) if wrong else None)


class Unread(str):
    """A dry run's problem that is an output the host does not read in full (hook_hosts: `output_problems`)."""


def _budget_problem() -> tuple[str, str]:
    return (MIDDLEWARE, f"{MIDDLEWARE}: the dry runs of its hooks took more than {DRY_RUN_BUDGET_S} s together: a hook "
                        f"runs inside every tool call of every trial, so keep each one fast (no sleeping, no scanning "
                        f"of large files)")


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


def dry_run_problems(deployments: list[Deployment], clock: Clock) -> list[tuple[str, str]]:
    """(5, run) on every host, one session of synthetic events in the host's own shape and order (hook_hosts:
    `session`: a prompt, two tool calls with their results, a stop and the stop after it), every event run through the
    hook the deployment installed, as the host runs it: a module that fails to import, or a hook that raises (at its
    first call or a later one, the state and the queue carried over), returns what the runner cannot translate,
    outlasts the runner's alarm or makes an output the host does not read fails here, instead of failing open on
    every event of every trial. The first failing hook is named, with the event it ran at."""
    for done in deployments:
        if done.result is None:
            continue                                  # its deployment failure is prompt_problems' to name
        wire = HOST_WIRES[done.host]
        env = _python_on_path(done.workspace.parent)
        seen: dict[str, int] = {}
        for event, payload in wire.session(str(done.workspace), "check-session"):
            seen[event] = seen.get(event, 0) + 1
            out, records, problem = run_hook(done, event, payload, env, clock)
            if problem == "budget":
                return [_budget_problem()]
            if problem is None:
                continue
            at = f"the {_ordinal(seen[event])} {event}" if seen[event] > 1 else event
            where = (f"{at} of a synthetic session" if (event, seen[event]) == ("SessionStart", 1) else
                     f"{at} of a synthetic session, the state carried over from the events before it")
            function = wire.events.get(event)
            hook = f"`{function}`" if function else "the queued context's hand-over"
            if isinstance(problem, dict) and problem.get("event") == "PreModelRequest" and problem.get("at"):
                return [(MIDDLEWARE, f"{MIDDLEWARE}: `before_model` fails in a dry run through {RUNNER} on "
                                     f"{wire.label}, where the runner calls it after {at} (a synthetic session): "
                                     f"{str(problem['error'])[:500]}; on {wire.label} only its context would be lost, "
                                     f"silently")]
            if isinstance(problem, Unread):
                return [(MIDDLEWARE, f"{MIDDLEWARE}: {hook} returns an output {wire.label} does not read in full, in a "
                                     f"dry run through {RUNNER} ({where}): {problem}; on {wire.label} that part would "
                                     f"be dropped, silently")]
            detail = str(problem["error"]) if isinstance(problem, dict) else str(problem)
            return [(MIDDLEWARE, f"{MIDDLEWARE}: {hook} fails in a dry run through {RUNNER} on {wire.label} ({where}): "
                                 f"{detail[:500]}; on {wire.label} this hook would fail open, silently")]
    return []


def memory_run_problems(adapter: Path, prompt: str, scratch: Path, clock: Clock) -> list[tuple[str, str]]:
    """(5, memory) on every host, session_start run by the deployed SessionStart hook of a fresh deployment whose
    MEMORY.md holds one entry (MEMORY_PROBE_ENTRY), in a session whose task is on the entry's topic
    (MEMORY_PROBE_TASK), hands the entry's mark (MEMORY_PROBE_MARK, in the memory alone) to the model as context: the
    memory channel is what session_start does, not a file name it mentions, so no static rule looks for one (it
    would refuse `MEMORY_FILE = Path("MEMORY.md")` and pass a hook that names the file and returns None)."""
    memory = f"{MEMORY_HEADER}\n\n## Agent Added Memories\n- {MEMORY_PROBE_ENTRY}\n"
    for host in HOSTS:
        done = deploy(adapter, prompt, host, scratch, memory=memory)
        if done.result is None:
            continue                                  # prompt_problems names the deployment's failure
        wire = HOST_WIRES[host]
        # The session's task (inp.task_text, the workspace's task.md) shares the entry's topic, not its mark.
        (done.workspace / "task.md").write_text(f"{MEMORY_PROBE_TASK}\n", encoding="utf-8")
        env = _python_on_path(done.workspace.parent)
        payload = wire.sample("SessionStart", str(done.workspace), "check-memory")
        out, records, problem = run_hook(done, "SessionStart", payload, env, clock)
        if problem == "budget":
            return [_budget_problem()]
        context = ((out if isinstance(out, dict) else {}).get("hookSpecificOutput") or {}).get("additionalContext")
        if problem or not isinstance(context, str) or MEMORY_PROBE_MARK not in context:
            detail = problem.get("error") if isinstance(problem, dict) else problem
            seen = str(detail) if detail else f"its context: {str(context)[:200]!r}"
            return [(MIDDLEWARE, f"{MIDDLEWARE}: `session_start`, run through {RUNNER} on {wire.label} on a {MEMORY} "
                                 f"with one entry, did not hand that entry to the model ({seen[:300]}): the long-term "
                                 f"memory reaches the model only through session_start, so the memory channel was "
                                 f"removed; read {MEMORY} from inp.adapter_dir and return its body as context")]
    return []


def script_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """(6) every .py under scripts/ and a skill's scripts/ compiles; a helper the prompt or a skill invokes exists,
    named as a deployment runs it."""
    errs = []
    for rel, src in tree.items():
        if rel.endswith(".py") and (RX["script"].fullmatch(rel) or RX["skill_script"].fullmatch(rel)):
            try:
                compile(src, rel, "exec")
            except (SyntaxError, ValueError) as exc:
                detail = (f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})" if isinstance(exc, SyntaxError)
                          else f"{type(exc).__name__}: {exc}")
                errs.append((rel, f"{rel} does not compile: {detail[:160]}"))
    have = {rel for rel in tree if RX["script"].fullmatch(rel) or RX["skill_script"].fullmatch(rel)}
    for rel, src in sorted(tree.items()):
        if not (RX["skill"].fullmatch(rel) or rel == ALWAYS_ON):
            continue
        for ref in sorted(set(RX["adapter_ref"].findall(src))):
            errs.append((rel, f"{rel} invokes `/adapter/{ref}`, but no deployment has /adapter: the adapter's copy is "
                              f"`{FOLDER}/` in the agent's workspace and its paths are relative to that folder, so "
                              f"name it `{ref}` (run from the folder) or `{FOLDER}/{ref}`"))
        refs = set(RX["script_ref"].findall(src)) | set(RX["skill_file_ref"].findall(src))
        for ref in sorted(refs):
            if ref not in have:
                errs.append((rel, f"{rel} invokes `{ref}` but that file is not in the harness; add it, or drop the "
                                  f"reference"))
    return errs


def skill_and_agent_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """(7) skills carry a description and a name equal to their directory; agents carry name and description, and
    their names stay apart as every host installs them."""
    errs = []
    for rel in sorted(tree):
        m = RX["skill"].fullmatch(rel)
        if not m:
            continue
        name, head = m.group(1), frontmatter(tree[rel]) or {}
        if _text_field(head, "description") is None:
            errs.append((rel, f"{rel} needs a `description:` string in its YAML frontmatter (`---` block at the top; "
                              f"here it is {_not_text(head, 'description')}), or the host drops the skill SILENTLY at "
                              f"load"))
        if head.get("name") is not None and not isinstance(head["name"], str):     # an empty `name:` is no name
            errs.append((rel, f"{rel} frontmatter `name:` is {_not_text(head, 'name')}: the host drops the skill at "
                              f"load; drop the `name:` line or make it `name: {name}`"))
        elif _text_field(head, "name") and head["name"] != name:
            errs.append((rel, f"{rel} frontmatter `name: {head['name']}` OVERRIDES the directory name at load, so the "
                              f"skill is advertised under another name; drop the `name:` line or make it `name: "
                              f"{name}`"))
    installed: dict[str, str] = {}
    for rel in sorted(tree):
        m = RX["agent"].fullmatch(rel)
        if not m:
            continue
        stem, head = m.group(1), frontmatter(tree[rel])
        if not AGENT_NAME.fullmatch(stem):
            errs.append((rel, f"{rel}: every host installs the role under its name, which must start with a letter "
                              f"and be at most 64 characters of [a-z0-9_-] (Codex spells a `-` as `_`, Claude Code an "
                              f"`_` as `-`); `{stem}` is not one, so the adapter does not deploy"))
            continue
        native = stem.replace("-", "_")
        if native in installed:
            errs.append((rel, f"{rel}: the hosts install the role as `{native}` (Codex) and `{native.replace('_', '-')}` "
                              f"(Claude Code), names {installed[native]} already takes, so the adapter does not "
                              f"deploy; rename one of them"))
        installed.setdefault(native, rel)
        if head is None:
            errs.append((rel, f"{rel} has no YAML frontmatter (or it is not valid YAML): a sub-agent file starts "
                              f"with `---`, `name: {stem}`, `description: ...`, `---`, then its instructions; without "
                              f"it the adapter does not deploy"))
            continue
        if _text_field(head, "name") is None or _text_field(head, "description") is None:
            errs.append((rel, f"{rel} frontmatter needs both `name:` and `description:` as strings (`name:` is "
                              f"{'a string' if _text_field(head, 'name') else _not_text(head, 'name')}, `description:` "
                              f"{'a string' if _text_field(head, 'description') else _not_text(head, 'description')}; "
                              f"the description is what the agent reads to decide whether to spawn the role)"))
        elif head["name"] != stem:
            errs.append((rel, f"{rel} frontmatter `name: {head['name']}` differs from the file stem `{stem}`; the "
                              f"role is installed under its name, so keep the two equal"))
    return errs


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    if not (adapter / ALWAYS_ON).is_file():
        # (8) Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": ALWAYS_ON, "problem": f"no ahe adapter at {adapter} (AGENTS.md missing or not visible)"}]
    tree, off = read_tree(adapter)
    # (1) off-surface files.
    issues = [{"path": rel, "problem": f"{rel} is off the AHE harness surface (AGENTS.md, MEMORY.md, middleware.py, "
                                       f"scripts/<name>.py, .agents/skills/<name>/SKILL.md and its scripts/, "
                                       f"agents/<name>.md, name = [a-z0-9][a-z0-9_-]*)"} for rel in off]
    middleware = middleware_problems(tree)
    with tempfile.TemporaryDirectory(prefix="ahe-check-") as scratch:
        deployments = deploy_all(adapter, tree.get(ALWAYS_ON, ""), Path(scratch) / "run")
        clock = Clock()
        dry_run = dry_run_problems(deployments, clock) if not middleware else []   # only what passes the static rules
        if not middleware and not dry_run:
            dry_run = memory_run_problems(adapter, tree.get(ALWAYS_ON, ""), Path(scratch) / "memory", clock)
        prompt = prompt_problems(tree, deployments)
    for rel, problem in (prompt + memory_problems(tree) + middleware + dry_run + script_problems(tree)
                         + skill_and_agent_problems(tree)):
        issues.append({"path": rel, "problem": problem})
    # (3) the context export is the always-on text.
    context = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if context != tree.get(ALWAYS_ON, ""):
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not AGENTS.md: it is the always-on text as the "
                       "framework exports it, so edit AGENTS.md and copy it"})
    return issues
