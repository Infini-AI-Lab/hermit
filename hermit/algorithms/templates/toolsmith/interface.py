"""Toolsmith adapter: learned instructions (ADAPTER.md) and learned tools (tools/<name>/ bundles) behind two fixed
exports. `tool_catalog` lists the admitted bundles; `tool_call` runs one through the confined runner the host binds as a
resource (`_resources["tool_runner"]`, confine.run's keyword signature) in the bound workspace — never on the bare host.
check.py imports `validate_bundle` and `html_marker` from this file by path, so the bundle rules live here once.

Nothing here touches tools/ at import time (the deploy check constructs the adapter in a subprocess); discovery happens
per call, so a learner's edits are seen by the next call. Only the standard library is imported at module level;
jsonschema is imported where it is used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parent
TOOLS = "tools"
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
RESERVED = frozenset({"write_file", "read_file", "replace_text", "run_command", "inspect_page", "wm", "tool_catalog",
                      "tool_call", "context_package"})
ENTRY_EXT = (".py", ".cjs", ".js", ".mjs", ".sh")
FILE_EXT = (".py", ".md", ".json", ".txt", ".js", ".cjs", ".mjs", ".sh")
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description", "enum", "default",
                         "items", "minimum", "maximum"})
MAX_DESCRIPTION = 1024
MAX_TOOLS = 8
DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 60, 90
TEXT_CHARS, STDERR_CHARS, TAIL_CHARS = 12_000, 2_000, 2_000
MAX_IMAGES, MAX_IMAGE_BYTES = 5, 2_000_000      # why: the agent shows at most five images per call, each ≤ 2 MB
MAX_ARGS_CHARS = 60_000                 # why: the arguments travel as one argv string; Linux caps one at 128 KiB
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
MOUNT = "/tool"                                 # where the runner binds the bundle directory, read-only

# A tool must never carry a page: the start of an HTML document anywhere, the brief's scene attribute outside a
# selector, the state hook outside a quoted string (a selector or a page.evaluate expression).
_DOCTYPE = re.compile(r"<!doctype\s+html", re.IGNORECASE)
_HTML_TAG = re.compile(r"<html[\s>]", re.IGNORECASE)
_SCENE_ATTR = re.compile(r"""(?<!\[)data-testid=["']scene["']""")
_STATE_HOOK = "__SHOWCASE__.getState"

# The three interpreters feed the JSON arguments to the program on stdin themselves, because the runner's argv is the
# only channel into the sandbox (there is no stdin parameter) and argv[0] must stay the token the runner maps.
_PY_BOOT = ("import subprocess, sys\n"
            "r = subprocess.run([sys.executable, '-I', '-B', sys.argv[1]], input=sys.argv[2].encode('utf-8'))\n"
            "sys.exit(r.returncode)\n")
_JS_BOOT = ("const r = require('child_process').spawnSync(process.execPath, [process.argv[1]], "
            "{input: process.argv[2], stdio: ['pipe', 'inherit', 'inherit']});"
            "if (r.error) console.error(String(r.error));"
            "process.exit(r.status === null ? 1 : r.status);")
_SH_BOOT = "printf '%s' \"$2\" | bash \"$1\""


# --- bundle rules -----------------------------------------------------------------------------------------------------
def html_marker(text: str) -> str | None:
    """The first HTML-page marker in a source text, as a problem sentence; None when there is none."""
    for number, line in enumerate(text.splitlines(), 1):
        if _DOCTYPE.search(line) or _HTML_TAG.search(line):
            return f"line {number} starts an HTML document; a tool must never carry a page"
        if _SCENE_ATTR.search(line):
            return f"line {number} carries the page's scene attribute outside a [data-testid=...] selector"
        if _bare_state_hook(line):
            return f"line {number} uses {_STATE_HOOK} outside a quoted string"
    return None


def _bare_state_hook(line: str) -> bool:
    start = 0
    while (i := line.find(_STATE_HOOK, start)) >= 0:
        before = line[:i]
        if not any(before.count(quote) % 2 for quote in "'\"`"):
            return True
        start = i + len(_STATE_HOOK)
    return False


def _unknown_keywords(schema: object) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    bad = set(schema) - SCHEMA_KEYS
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for sub in properties.values():
            bad |= _unknown_keywords(sub)
    return bad | _unknown_keywords(schema.get("items"))


def _parameter_problems(parameters: object) -> list[str]:
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return ["parameters must be a JSON Schema object with \"type\": \"object\""]
    problems = []
    unknown = _unknown_keywords(parameters)
    if unknown:
        problems.append(f"parameters use keywords outside the allowed set: {sorted(unknown)}")
    if not isinstance(parameters.get("properties", {}), dict):
        problems.append("parameters.properties must be an object")
    if parameters.get("required", []) != []:
        problems.append("every parameter must be optional: \"required\" must be [] (a tool runs with no arguments)")
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as exc:
        problems.append(f"parameters is not a valid JSON Schema: {exc.message}")
    return problems


def _file_name_problem(field: str, value: object) -> str | None:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or value.startswith("."):
        return f"{field} must be a plain file name inside the bundle"
    if Path(value).suffix not in ENTRY_EXT:
        return f"{field} {value!r} must end in one of {', '.join(ENTRY_EXT)}"
    return None


def validate_bundle(root: Path, name: str) -> list[dict]:
    """Issues [{path, check: 6, problem}] for tools/<name>/; empty when the bundle is admissible. Every issue names
    the bundle's TOOL.json (an existing file) so the Trainer's repair drops that bundle and nothing else."""
    root = Path(root)
    bundle = root / TOOLS / name
    manifest = bundle / "TOOL.json"
    rel = f"{TOOLS}/{name}/TOOL.json"
    issues: list[dict] = []

    def issue(problem: str) -> None:
        issues.append({"path": rel, "check": 6, "problem": problem})

    if not manifest.is_file():
        first = next((p for p in sorted(bundle.rglob("*")) if p.is_file()), None)
        return [{"path": first.relative_to(root).as_posix() if first else "", "check": 6, "problem": "no TOOL.json"}]
    try:
        spec = json.loads(manifest.read_text("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return [{"path": rel, "check": 6, "problem": f"TOOL.json is not valid JSON: {exc}"}]
    if not isinstance(spec, dict):
        return [{"path": rel, "check": 6, "problem": "TOOL.json must be a JSON object"}]
    if not NAME_RE.fullmatch(name):
        issue(f"directory name {name!r} is not a tool name (^[a-z][a-z0-9_]{{0,63}}$)")
    if spec.get("name") != name:
        issue(f"\"name\" must equal the directory name {name!r}")
    if name in RESERVED:
        issue(f"{name!r} is a reserved name")
    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        issue("\"description\" must be a non-empty string")
    elif len(description) > MAX_DESCRIPTION:
        issue(f"\"description\" has {len(description)} characters; at most {MAX_DESCRIPTION}")
    parameter_problems = _parameter_problems(spec.get("parameters"))
    for problem in parameter_problems:
        issue(problem)
    problem = _file_name_problem("\"entry\"", spec.get("entry"))
    if problem:
        issue(problem)
    elif not (bundle / spec["entry"]).is_file():
        issue(f"\"entry\" names {spec['entry']!r}, which does not exist in the bundle")
    if spec.get("test") is not None:
        problem = _file_name_problem("\"test\"", spec.get("test"))
        if problem:
            issue(problem)
        elif not (bundle / spec["test"]).is_file():
            issue(f"\"test\" names {spec['test']!r}, which does not exist in the bundle")
    example = spec.get("example_args", {})
    if not isinstance(example, dict):
        issue("\"example_args\" must be a JSON object")
    elif not parameter_problems:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
        try:
            Draft202012Validator(spec["parameters"]).validate(example)
        except ValidationError as exc:
            issue(f"\"example_args\" do not fit \"parameters\": {exc.message}")
    timeout = spec.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_S:
        issue(f"\"timeout_s\" must be an integer from 1 to {MAX_TIMEOUT_S}")
    for path in sorted(bundle.rglob("*")):
        inner = path.relative_to(bundle)
        if "__pycache__" in inner.parts:
            continue
        if any(part.startswith(".") for part in inner.parts):
            issue(f"{inner.as_posix()}: no path component may start with '.'")
            continue
        if path.is_symlink():
            issue(f"{inner.as_posix()}: symbolic links are not allowed")
            continue
        if not path.is_file():
            continue
        if path.suffix not in FILE_EXT:
            issue(f"{inner.as_posix()}: only {', '.join(FILE_EXT)} files belong in a bundle")
            continue
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            issue(f"{inner.as_posix()}: not UTF-8 text")
            continue
        marker = html_marker(text)
        if marker:
            issue(f"{inner.as_posix()}: {marker}")
    return issues


def catalog(root: Path) -> tuple[list[dict], list[dict]]:
    """(admitted bundles as their normalised TOOL.json, rejected [{name, problem}]). Directories without TOOL.json
    and names starting with '_' or '.' are not tools; at most MAX_TOOLS are admitted, alphabetically."""
    root = Path(root)
    tools = root / TOOLS
    admitted: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    if not tools.is_dir():
        return admitted, rejected
    for entry in sorted(tools.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")) or not (entry / "TOOL.json").is_file():
            continue
        issues = validate_bundle(root, entry.name)
        if issues:
            rejected.append({"name": entry.name, "problem": "; ".join(i["problem"] for i in issues)})
            continue
        if entry.name.casefold() in seen:
            rejected.append({"name": entry.name, "problem": "duplicate tool name"})
            continue
        if len(admitted) >= MAX_TOOLS:
            rejected.append({"name": entry.name, "problem": "too many tools"})
            continue
        seen.add(entry.name.casefold())
        spec = json.loads((entry / "TOOL.json").read_text("utf-8"))
        admitted.append({"name": entry.name, "description": spec["description"], "parameters": spec["parameters"],
                         "entry": spec["entry"], "test": spec.get("test"), "example_args": spec.get("example_args", {}),
                         "timeout_s": int(spec.get("timeout_s", DEFAULT_TIMEOUT_S))})
    return admitted, rejected


# --- execution protocol -----------------------------------------------------------------------------------------------
def program_argv(entry: str, args: dict | None) -> list[str]:
    """The runner argv for a bundle entry: the interpreter token by extension, a bootstrap that pipes the JSON
    arguments to the program's stdin, the program at /tool/<entry>, the arguments. The runner maps the token to the
    host interpreter (python3 → the venv python, node, bash)."""
    payload = json.dumps(args or {}, ensure_ascii=True, separators=(",", ":"))
    target = f"{MOUNT}/{entry}"
    suffix = Path(entry).suffix
    if suffix == ".py":
        return ["python3", "-I", "-B", "-c", _PY_BOOT, target, payload]
    if suffix in (".cjs", ".js", ".mjs"):
        return ["node", "-e", _JS_BOOT, target, payload]
    if suffix == ".sh":
        return ["bash", "-c", _SH_BOOT, "toolsmith", target, payload]
    raise ValueError(f"no interpreter for {entry!r}")


def last_json_object(stdout: str) -> dict | None:
    """The last stdout line that parses as a JSON object."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _image_check(item: str, workspace: Path) -> tuple[str | None, str | None]:
    """(relative path, None) when `item` is a PNG/JPEG regular file inside the workspace reached without a symlink;
    (None, problem) otherwise."""
    real_ws = Path(os.path.realpath(workspace))
    given = Path(os.path.normpath(item))
    if given.is_absolute():
        for base in (real_ws, Path(workspace)):
            try:
                rel = given.relative_to(base)
                break
            except ValueError:
                continue
        else:
            return None, "outside the workspace"
    else:
        rel = given
    if not rel.parts or rel.parts[0] == "..":
        return None, "outside the workspace"
    node = real_ws
    for part in rel.parts:
        node = node / part
        if node.is_symlink():
            return None, "a symbolic link"
    try:
        info = os.lstat(node)
    except OSError:
        return None, "does not exist"
    if not stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    if info.st_size > MAX_IMAGE_BYTES:
        return None, f"{info.st_size} bytes, more than {MAX_IMAGE_BYTES}"
    with node.open("rb") as stream:
        head = stream.read(8)
    if not head.startswith(IMAGE_MAGIC):
        return None, "not a PNG or JPEG file"
    return rel.as_posix(), None


def _images(value: object, workspace: Path) -> tuple[list[str], list[dict]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [{"path": "", "problem": "images must be a list of file paths"}]
    accepted: list[str] = []
    rejected: list[dict] = []
    for item in value:
        if not isinstance(item, str) or not item:
            rejected.append({"path": str(item), "problem": "not a path"})
            continue
        rel, problem = _image_check(item, workspace)
        if problem:
            rejected.append({"path": item, "problem": problem})
        elif rel in accepted:
            continue
        elif len(accepted) >= MAX_IMAGES:
            rejected.append({"path": item, "problem": f"more than {MAX_IMAGES} images in one call"})
        else:
            accepted.append(rel)
    return accepted, rejected


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    note = f"\n[clipped to {limit} characters]"
    return text[:limit - len(note)] + note


def _tails(stdout: str, stderr: str) -> str:
    return f"\nstdout (tail):\n{stdout[-TAIL_CHARS:]}\nstderr (tail):\n{stderr[-TAIL_CHARS:]}"


# --- exports ----------------------------------------------------------------------------------------------------------
def tool_catalog(_resources=None) -> dict:
    """{tools: [{name, description, parameters}], rejected: [{name, problem}]} for the bundles under tools/ now."""
    admitted, rejected = catalog(ROOT)
    return {"tools": [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]}
                      for t in admitted], "rejected": rejected}


def tool_call(name: str, args: dict | None = None, _resources=None) -> dict:
    """Run tools/<name>/ through the bound confined runner in the bound workspace. Returns {text, images (paths
    relative to the workspace), exit_code, stderr_tail, seconds, timed_out, lock_wait_s, error, images_rejected,
    browser_launched}; failures come back as text with `error` set, never as exceptions, except when no runner or no
    workspace is bound (RuntimeError: a learned program never runs on the bare host)."""
    resources = _resources or {}
    runner = resources.get("tool_runner")
    if not callable(runner):
        raise RuntimeError("no confined runner bound")
    deployment = resources.get("deployment")
    workspace = resources.get("workspace") or (deployment.get("workspace") if isinstance(deployment, dict) else None)
    if not workspace:
        raise RuntimeError("no confined runner bound: the workspace resource is missing")
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise RuntimeError(f"the workspace is not a directory: {workspace}")
    result = {"text": "", "images": [], "exit_code": None, "stderr_tail": "", "seconds": None, "timed_out": False,
              "lock_wait_s": None, "error": None, "images_rejected": [], "browser_launched": None}

    def failure(text: str, error: str) -> dict:
        return {**result, "text": text, "images": [], "error": error}

    admitted, rejected = catalog(ROOT)
    bundle = next((t for t in admitted if t["name"] == name), None)
    if bundle is None:
        why = next((r["problem"] for r in rejected if r["name"] == name), None)
        names = ", ".join(t["name"] for t in admitted) or "none"
        return failure(f"Unknown tool {name!r}" + (f" (rejected: {why})" if why else "") + f". Tools: {names}.",
                       "unknown tool")
    args = args if args is not None else {}
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
    try:
        Draft202012Validator(bundle["parameters"]).validate(args)
    except ValidationError as exc:
        return failure(f"Invalid arguments for {name}: {exc.message}", "invalid arguments")
    argv = program_argv(bundle["entry"], args)
    if len(argv[-1]) > MAX_ARGS_CHARS:
        return failure(f"Arguments for {name} serialise to {len(argv[-1])} characters; at most {MAX_ARGS_CHARS}",
                       "arguments too large")
    run = runner(argv=argv, cwd=str(workspace), timeout_s=bundle["timeout_s"],
                 ro=[[str(ROOT / TOOLS / name), MOUNT]], lock="shared")
    run = run if isinstance(run, dict) else {}
    stdout, stderr = str(run.get("stdout") or ""), str(run.get("stderr") or "")
    result.update(exit_code=run.get("exit_code"), seconds=run.get("seconds"), timed_out=bool(run.get("timed_out")),
                  lock_wait_s=run.get("lock_wait_s"), stderr_tail=stderr[-STDERR_CHARS:],
                  browser_launched=run.get("browser_launched"))
    if run.get("error"):
        return failure(f"{name} did not run: {run['error']}", str(run["error"]))
    if result["timed_out"]:
        return failure(f"{name} timed out after {bundle['timeout_s']} s." + _tails(stdout, stderr), "timeout")
    if result["exit_code"] != 0:
        return failure(f"{name} exited with code {result['exit_code']}." + _tails(stdout, stderr),
                       f"exit {result['exit_code']}")
    reply = last_json_object(stdout)
    if reply is None:
        return failure(f"{name} printed no JSON object line (the last stdout line must be the result)."
                       + _tails(stdout, stderr), "no result line")
    if not isinstance(reply.get("text"), str):
        return failure(f"{name}'s result has no \"text\" string." + _tails(stdout, stderr), "bad result")
    images, images_rejected = _images(reply.get("images"), workspace)
    return {**result, "text": _clip(reply["text"], TEXT_CHARS), "images": images, "images_rejected": images_rejected}
