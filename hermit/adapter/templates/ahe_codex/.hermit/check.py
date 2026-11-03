"""Template check for `ahe-codex` adapters: the tree is the AHE harness H (arXiv 2604.25850 §3.1) in the layout Codex
reads from its workspace root, nothing lies off that surface, the seed system prompt survives (the paper's
non-deletable seed, §3.3), the memory channel is intact, the middleware is a module the adapter's hook runner can
load and call where the agent runs, and ADAPTER.md is AGENTS.md (the framework's context export is the always-on
text; AGENTS.md is the file Codex reads).

The rules on the files are stated here with the stdlib only. Two rules run what they check: the AGENTS.md a
deployment writes is measured by deploying a copy of the adapter as the framework does (hermit.hosts.codex.prepare,
what `hermit deploy` and the framework's Codex agent run; the framework is importable wherever check_spec runs this),
and every hook of middleware.py is called once by the adapter's own hook_runner.py, as Codex calls it on an event.

The surface:
    AGENTS.md                              system prompt + tool guidance: never empty, within the 32,768 bytes Codex
                                           reads once deployed (the deployment adds its managed block, a note on the
                                           adapter's folder, the list of the adapter's other files and two bytes per
                                           nested heading), the three seed rules present (whitespace-normalised)
    MEMORY.md                              long-term memory: present, starts with `# Long-term memory`
    middleware.py                          the hook pipeline: compiles; defines the five hooks (session_start,
                                           before_tool, after_tool, stop, before_model; the runner calls all five),
                                           each `(inp, state)`; other top-level functions are private helpers
                                           (`_name`); imports stdlib or hermit_hooks only; none of the network/process
                                           modules, by import or by call; session_start (or a helper it calls) reads
                                           MEMORY.md (the memory channel is never removed); every hook runs once
                                           through hook_runner.py without an error; and session_start, run on a
                                           MEMORY.md holding an entry, hands that entry to the model
    scripts/<name>.py                      a tool implementation; must compile
    .agents/skills/<name>/SKILL.md         frontmatter `description:` required; `name:` optional, must equal the dir
    .agents/skills/<name>/scripts/<file>   a helper carried with its skill; a .py must compile
    agents/<name>.md                       a sub-agent (Codex agent role): frontmatter `name:` (= the file stem) and
                                           `description:` required; the stem is a Codex agent name, unique once its
                                           `-` are `_`
A script AGENTS.md or a SKILL.md invokes must be in the tree, named as a deployment lets the agent run it: relative
to the adapter's folder (`python3 scripts/<x>.py`), or through that folder (`python3 hermit/ahe-codex/scripts/<x>.py`).
The fixed files (hooks.json, hook_runner.py, hermit_hooks.py) and the placeholders of the on-demand directories
(scripts/README.md, .agents/skills/README.md, agents/README.md) are not part of the tree.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ALWAYS_ON = "AGENTS.md"
ALWAYS_ON_MAX_BYTES = 32_000          # the nominal cap; the deployed form decides (see prompt_problems)
CODEX_LIMIT_BYTES = 32_768            # Codex's project_doc_max_bytes by default: a longer deployment is refused
FOLDER = "hermit/ahe-codex"           # where a deployment puts the adapter's copy in the workspace (hermit/<id>/)
MEMORY = "MEMORY.md"
MEMORY_HEADER = "# Long-term memory"
MIDDLEWARE = "middleware.py"
RUNNER = "hook_runner.py"
HOOKS = ("session_start", "before_tool", "after_tool", "stop", "before_model")
# The host event the runner is started for, for each hook (hermit_hooks.EVENTS).
HOOK_EVENTS = {"session_start": "SessionStart", "before_tool": "PreToolUse", "after_tool": "PostToolUse",
               "stop": "Stop", "before_model": "PreModelRequest"}
# `hermit_hooks` is the interface beside the runner. `hermit.runtime.hooks`, the framework module it was before, is
# still accepted: adapters trained then import it as a fallback inside a try (a dead branch under this runner).
HOOK_MODULES = frozenset({"hermit.runtime.hooks", "hermit_hooks"})
# A middleware runs inside every tool call of a trial: no network, no process spawning, no event loop, and no way
# around the import rule (importlib, ctypes). The sandbox already denies the network on the learner side; where the
# agent runs the rule is this check, static: the runner's own guards are its alarm and fail-open.
FORBIDDEN_MODULES = frozenset({"socket", "http", "urllib", "ssl", "ftplib", "smtplib", "xmlrpc", "asyncio", "subprocess",
                               "ctypes", "multiprocessing", "pty", "importlib"})
FORBIDDEN_CALLS = frozenset({"__import__", "importlib.import_module", "os.system", "os.popen", "os.fork", "os.forkpty",
                             "os.posix_spawn", "os.posix_spawnp"})
FORBIDDEN_CALL_PREFIXES = ("os.exec", "os.spawn")
ROOT = Path(__file__).resolve().parents[1]      # the template root this check is loaded from (its fixed files)
DRY_RUN_TIMEOUT_S = 40                # one hook's dry run: the runner's own alarm (28 s) fires first
# The memory run: one entry in a copy of the adapter's MEMORY.md, carrying a mark found nowhere else, and a task on
# the entry's topic without the mark. session_start must hand the mark to the model: only the memory holds it, so a
# hook that echoes the task fails, and one that hands over the entries relevant to the task passes.
MEMORY_PROBE_MARK = "ahe-codex-memory-probe-3f9c1a"
MEMORY_PROBE_ENTRY = f"When a table reports a value, check its units before answering. [{MEMORY_PROBE_MARK}]"
MEMORY_PROBE_TASK = "Read the table that reports the value, and check its units before answering."
# One hook call as Codex makes it, by the template's runner on the adapter's middleware: argv runner, event, adapter.
DRY_RUN = ("import importlib.util, sys\n"
           "spec = importlib.util.spec_from_file_location('ahe_codex_hook_runner', sys.argv[1])\n"
           "runner = importlib.util.module_from_spec(spec)\n"
           "spec.loader.exec_module(runner)\n"
           "sys.exit(runner.run(sys.argv[2], sys.stdin.read(), adapter=sys.argv[3]))\n")
# The paper's seed system prompt (Appendix B.1; $AHE/agents/code_agent_simple/systemprompt.md), the three rules, as
# the template's AGENTS.md carries them: the Evolve Agent may add to the prompt, never delete or reword these.
SEED_RULES = (
    "Prefer short replies; use the tool for actions.",
    "Before commands that delete or overwrite important data, state briefly what they do.",
    "Long-running processes: run them in the background and poll their output instead of blocking.",
)
# The framework's and the template's own files, and the placeholders: not part of the harness tree.
OWN = frozenset({"ADAPTER.md", "exports.yaml", "README.md", "hooks.json", RUNNER, "hermit_hooks.py"})   # and .hermit/
STUBS = frozenset({"scripts/README.md", ".agents/skills/README.md", "agents/README.md"})
NAME = r"[a-z0-9][a-z0-9_\-]*"
# Codex installs a role under its name (`-` becomes `_`): a letter first, at most 64 characters (hosts/codex.py).
AGENT_NAME = re.compile(r"[a-z][a-z0-9_\-]{0,63}")
RX = {"skill": re.compile(rf"\.agents/skills/({NAME})/SKILL\.md"),
      "skill_script": re.compile(rf"\.agents/skills/{NAME}/scripts/[A-Za-z0-9_.\-]+"),
      "agent": re.compile(rf"agents/({NAME})\.md"),
      "script": re.compile(r"scripts/[A-Za-z0-9_.\-]+\.py"),       # the manifest's learnable glob, scripts/*.py
      # `python3 scripts/x.py`, `python3 ./scripts/x.py`, `python3 hermit/ahe-codex/scripts/x.py`,
      # `python3 .agents/skills/<n>/scripts/x.py`: the file must exist (the capture is the adapter-relative path).
      "script_ref": re.compile(rf"(?:python3?\s+)(?:\./)?(?:{re.escape(FOLDER)}/)?"
                               rf"((?:\.agents/skills/[\w\-]+/)?scripts/[\w.\-]+\.py)"),
      # Any mention of a skill helper, with or without an interpreter: the file must exist.
      "skill_file_ref": re.compile(r"(\.agents/skills/[\w\-]+/scripts/[\w\-][\w.\-]*)"),
      # The task container's /adapter of an earlier deployment: no deployment has it now.
      "adapter_ref": re.compile(r"/adapter/((?:\.agents/skills/[\w\-]+/)?scripts/[\w.\-]+)")}
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.S)


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


def frontmatter(text: str) -> dict[str, str] | None:
    """The `key: value` lines of a leading YAML frontmatter block (flat keys only; a nested value is kept as its
    first line), or None when the file has no such block."""
    m = FRONTMATTER.match(text)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z_][\w\-]*", k.strip()):
            out[k.strip()] = v.strip().strip("'\"")
    return out


def deployed_size(adapter: Path, prompt: str) -> tuple[int | None, str, str]:
    """The bytes of the AGENTS.md a deployment writes for this adapter with `prompt` as its AGENTS.md (and so as
    ADAPTER.md, which the Evolver keeps equal to it): a copy of the adapter deployed into a scratch workspace by the
    framework's own deployment, with no size limit. When it does not deploy: (None, why, the adapter file `why`
    names or ""), found by check_spec's own rule for a deploy failure (hermit.spec._path_in), so that repair reverts
    that file alone. (_path_in also looks under .hermit/, which a deployment names only when a metadata file is at
    fault; check_spec runs this check only when every one is the template's.)"""
    try:
        from hermit.hosts.codex import prepare
        from hermit.spec import _path_in
    except ImportError as exc:
        return None, f"the framework's deployment cannot be imported here ({type(exc).__name__}: {exc})", ""
    with tempfile.TemporaryDirectory(prefix="ahe-codex-check-") as scratch:
        copy, workspace = Path(scratch) / "adapter", Path(scratch) / "workspace"
        shutil.copytree(adapter, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        for name in (ALWAYS_ON, "ADAPTER.md"):
            (copy / name).write_text(prompt, encoding="utf-8")
        try:
            prepare(copy, workspace, sys.executable, context_limit=None)
        except Exception as exc:                      # noqa: BLE001 - the deployment's own refusal, named
            why = f"{type(exc).__name__}: {exc}".replace(str(copy) + "/", "")[:600]
            return None, why, _path_in(adapter, why)
        return len((workspace / ALWAYS_ON).read_bytes()), "", ""


def prompt_problems(adapter: Path, tree: dict[str, str]) -> list[tuple[str, str]]:
    """(2) AGENTS.md: present, non-empty, within what Codex reads once deployed, the seed rules intact."""
    body = tree.get(ALWAYS_ON, "")
    errs = []
    if not body.strip():
        return [(ALWAYS_ON, f"{ALWAYS_ON} is empty: it is the system prompt, the one always-on file, so an empty one "
                            f"is a harness that does not exist")]
    size = len(body.encode("utf-8"))
    deployed, why, named = deployed_size(adapter, body)
    if deployed is None:
        errs.append((named, f"the adapter does not deploy to Codex, so {ALWAYS_ON} cannot be measured as the agent "
                            f"reads it: {why}"))
    else:
        overhead = deployed - size
        cap = min(ALWAYS_ON_MAX_BYTES, CODEX_LIMIT_BYTES - overhead)
        if size > cap:
            errs.append((ALWAYS_ON, f"{ALWAYS_ON} is {size} bytes, over the {cap}-byte cap: the deployment adds "
                                    f"{overhead} bytes around it (its managed block, the note on the adapter's folder, "
                                    f"the list of the adapter's other files and two bytes per nested heading), making "
                                    f"{deployed} bytes, and a deployment past the {CODEX_LIMIT_BYTES} bytes Codex "
                                    f"reads is refused; it is injected whole into EVERY turn"))
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


def _reads_memory(module: ast.Module) -> bool:
    """Whether `session_start`, or a module-level function it calls (transitively), holds a string constant naming
    MEMORY.md outside its docstring, directly or through a module-level name bound to such a constant. A static
    first look: memory_run_problems runs session_start to see that the memory reaches the model."""
    functions = {n.name: n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    constants = set()
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and MEMORY in value.value:
                constants |= {t.id for t in targets if isinstance(t, ast.Name)}
    seen, todo = set(), ["session_start"]
    while todo:
        name = todo.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        body = functions[name].body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]                           # a docstring that names the file reads nothing
        for node in (n for statement in body for n in ast.walk(statement)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and MEMORY in node.value:
                return True
            if isinstance(node, ast.Name) and node.id in constants:
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                todo.append(node.func.id)
    return False


def middleware_problems(tree: dict[str, str]) -> list[tuple[str, str]]:
    """(5) middleware.py: compiles, is shaped as the hook interface, imports nothing the runner cannot provide where
    the agent runs, and keeps the memory channel."""
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
            errs.append((MIDDLEWARE, f"{MIDDLEWARE} defines no `{hook}`: the hook runner calls all five hooks, one per "
                                     f"Codex event hooks.json registers; an unused hook is "
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
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            called = _call_name(node)
            if called in FORBIDDEN_CALLS or called.startswith(FORBIDDEN_CALL_PREFIXES):
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
    if "session_start" in defined and not _reads_memory(module):
        errs.append((MIDDLEWARE, f"{MIDDLEWARE}: session_start no longer reads {MEMORY} (neither it nor a helper it "
                                 f"calls names the file; a comment or docstring does not count): the long-term memory "
                                 f"reaches the model only through session_start, so the memory channel was removed; "
                                 f"read {MEMORY} from inp.adapter_dir and return its body as context"))
    return errs


def _payload(hook: str, workspace: Path) -> dict:
    """A synthetic Codex hook payload for `hook`'s event, in a session of its own (an empty state)."""
    event = HOOK_EVENTS[hook]
    payload = {"hook_event_name": event, "session_id": f"check-{hook}", "cwd": str(workspace),
               "transcript_path": None, "model": "check"}
    if hook != "session_start":
        payload["turn_id"] = "check"
    if hook in ("before_tool", "after_tool"):
        payload.update({"tool_name": "Bash", "tool_input": {"command": "true"}, "tool_use_id": "check"})
    if hook == "after_tool":
        payload["tool_response"] = ""
    if hook == "stop":
        payload.update({"last_assistant_message": "", "stop_hook_active": False})
    return payload


def dry_run_problems(adapter: Path) -> list[tuple[str, str]]:
    """(5, run) every hook of middleware.py called once by the template's hook_runner.py as Codex calls it on the
    hook's event, in a process of its own (a synthetic payload on stdin, the runner's stdout kept from this check's,
    its logs in a scratch workspace): a module that fails to import, or a hook that raises, returns what the runner
    cannot translate or outlasts the runner's alarm fails here, instead of failing open on every event of every
    trial. The first failing hook is named."""
    runner = ROOT / RUNNER if (ROOT / RUNNER).is_file() else adapter / RUNNER
    if not runner.is_file() or not (runner.parent / "hermit_hooks.py").is_file():
        return []                                     # a fixed file is missing: check_spec's check 1 names it
    env = {k: v for k, v in os.environ.items() if k != "HERMIT_HOOK_LOGS"}
    with tempfile.TemporaryDirectory(prefix="ahe-codex-dry-run-") as scratch:
        workspace = Path(scratch)
        for hook in HOOKS:
            event = HOOK_EVENTS[hook]
            try:
                proc = subprocess.run([sys.executable, "-B", "-c", DRY_RUN, str(runner), event, str(adapter)],
                                      cwd=str(adapter), env=env, input=json.dumps(_payload(hook, workspace)),
                                      capture_output=True, text=True, timeout=DRY_RUN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return [(MIDDLEWARE, f"{MIDDLEWARE}: `{hook}` ran past {DRY_RUN_TIMEOUT_S} s in a dry run through "
                                     f"{RUNNER} ({event}) although the runner stops a hook after 28 s")]
            audit = workspace / ".hermit-hooks" / "hook-calls.jsonl"
            lines = audit.read_text(encoding="utf-8").splitlines() if audit.is_file() else []
            line = json.loads(lines[-1]) if lines else {}
            problem = None
            if proc.returncode != 0 or not lines or line.get("event") != event:
                problem = f"the runner exited {proc.returncode} without its audit line: {proc.stderr.strip()[-300:]}"
            elif line.get("error"):
                problem = str(line["error"])
            elif proc.stdout.strip():
                try:
                    json.loads(proc.stdout)
                except ValueError:
                    problem = f"the runner's output is not one JSON document: {proc.stdout.strip()[:200]!r}"
            if problem:
                return [(MIDDLEWARE, f"{MIDDLEWARE}: `{hook}` fails in a dry run through {RUNNER} ({event}, a "
                                     f"synthetic payload, an empty state): {problem[:500]}; on the host this hook "
                                     f"would fail open on every call, silently")]
    return []


def memory_run_problems(adapter: Path) -> list[tuple[str, str]]:
    """(5, memory) session_start, called by the template's hook_runner.py on a copy of the adapter whose MEMORY.md
    holds one entry (MEMORY_PROBE_ENTRY), in a session whose task is on the entry's topic (MEMORY_PROBE_TASK), hands
    the entry's mark (MEMORY_PROBE_MARK, in the memory alone) to the model as context: the memory channel is what
    session_start does, not a file name it mentions (a hook that names MEMORY.md and returns None, or returns the
    task, passes the static rule)."""
    runner = ROOT / RUNNER if (ROOT / RUNNER).is_file() else adapter / RUNNER
    if not runner.is_file() or not (runner.parent / "hermit_hooks.py").is_file():
        return []                                     # a fixed file is missing: check_spec's check 1 names it
    env = {k: v for k, v in os.environ.items() if k != "HERMIT_HOOK_LOGS"}
    with tempfile.TemporaryDirectory(prefix="ahe-codex-memory-run-") as scratch:
        copy, workspace = Path(scratch) / "adapter", Path(scratch) / "workspace"
        shutil.copytree(adapter, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", ".hermit-hooks"))
        (copy / MEMORY).write_text(f"{MEMORY_HEADER}\n\n## Agent Added Memories\n- {MEMORY_PROBE_ENTRY}\n",
                                   encoding="utf-8")
        workspace.mkdir()
        # The session's task (inp.task_text, the workspace's task.md) shares the entry's topic, not its mark.
        (workspace / "task.md").write_text(f"{MEMORY_PROBE_TASK}\n", encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-B", "-c", DRY_RUN, str(runner), "SessionStart", str(copy)],
                                  cwd=str(copy), env=env, input=json.dumps(_payload("session_start", workspace)),
                                  capture_output=True, text=True, timeout=DRY_RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return [(MIDDLEWARE, f"{MIDDLEWARE}: `session_start` ran past {DRY_RUN_TIMEOUT_S} s on a {MEMORY} with "
                                 f"one entry")]
        try:
            out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            out = {}
        context = ((out if isinstance(out, dict) else {}).get("hookSpecificOutput") or {}).get("additionalContext")
        if not isinstance(context, str) or MEMORY_PROBE_MARK not in context:
            return [(MIDDLEWARE, f"{MIDDLEWARE}: `session_start`, run through {RUNNER} on a {MEMORY} with one entry, "
                                 f"did not hand that entry to the model (its context: {str(context)[:200]!r}): the "
                                 f"long-term memory reaches the model only through session_start, so the memory "
                                 f"channel was removed; read {MEMORY} from inp.adapter_dir and return its body as "
                                 f"context")]
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
    their names stay apart as Codex installs them."""
    errs = []
    for rel in sorted(tree):
        m = RX["skill"].fullmatch(rel)
        if not m:
            continue
        name, head = m.group(1), frontmatter(tree[rel]) or {}
        if not head.get("description"):
            errs.append((rel, f"{rel} needs a `description:` in its YAML frontmatter (`---` block at the top), or Codex "
                              f"drops the skill SILENTLY at load"))
        if head.get("name") and head["name"] != name:
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
            errs.append((rel, f"{rel}: Codex installs the role under its name, which must start with a letter and be "
                              f"at most 64 characters of [a-z0-9_-] (a `-` becomes `_`); `{stem}` is not one, so the "
                              f"adapter does not deploy"))
            continue
        native = stem.replace("-", "_")
        if native in installed:
            errs.append((rel, f"{rel}: Codex installs the role as `{native}` (a `-` becomes `_`), the name "
                              f"{installed[native]} already takes, so the adapter does not deploy; rename one of them"))
        installed.setdefault(native, rel)
        if head is None:
            errs.append((rel, f"{rel} has no YAML frontmatter: a sub-agent file starts with `---`, `name: {stem}`, "
                              f"`description: ...`, `---`, then its instructions; without it the adapter does not "
                              f"deploy"))
            continue
        if not head.get("name") or not head.get("description"):
            errs.append((rel, f"{rel} frontmatter needs both `name:` and `description:` (the description is what "
                              f"the agent reads to decide whether to spawn the role)"))
        elif head["name"] != stem:
            errs.append((rel, f"{rel} frontmatter `name: {head['name']}` differs from the file stem `{stem}`; the "
                              f"role is installed under its name, so keep the two equal"))
    return errs


def check(adapter_path: str) -> list[dict]:
    adapter = Path(adapter_path).resolve()
    if not (adapter / ALWAYS_ON).is_file():
        # (8) Never pass an adapter this check cannot see (under bubblewrap, /tmp is an empty tmpfs).
        return [{"path": ALWAYS_ON, "problem": f"no ahe-codex adapter at {adapter} (AGENTS.md missing or not visible)"}]
    tree, off = read_tree(adapter)
    # (1) off-surface files.
    issues = [{"path": rel, "problem": f"{rel} is off the AHE harness surface (AGENTS.md, MEMORY.md, middleware.py, "
                                       f"scripts/<name>.py, .agents/skills/<name>/SKILL.md and its scripts/, "
                                       f"agents/<name>.md, name = [a-z0-9][a-z0-9_-]*)"} for rel in off]
    middleware = middleware_problems(tree)
    dry_run = dry_run_problems(adapter) if not middleware else []    # run only what passes the static rules
    if not middleware and not dry_run:
        dry_run = memory_run_problems(adapter)
    for rel, problem in (prompt_problems(adapter, tree) + memory_problems(tree) + middleware + dry_run
                         + script_problems(tree) + skill_and_agent_problems(tree)):
        issues.append({"path": rel, "problem": problem})
    # (3) the context export is the always-on text.
    context = (adapter / "ADAPTER.md").read_text(encoding="utf-8") if (adapter / "ADAPTER.md").is_file() else ""
    if context != tree.get(ALWAYS_ON, ""):
        issues.append({"path": "ADAPTER.md", "problem": "ADAPTER.md is not AGENTS.md: it is the always-on text as the "
                       "framework exports it, so edit AGENTS.md and copy it"})
    return issues
