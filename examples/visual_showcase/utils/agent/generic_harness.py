"""The generic acting agent's harness (experiment 2): four primitives over the run's workspace — write_file, read_file,
replace_text and run_command — plus the adapter's learned tools (toolsmith `tool_catalog`/`tool_call`) and the read-only
`wm` tool of an MWM adapter. Nothing here knows how a page is rendered: the only channel back to the model is that PNG
files a command or a learned tool writes inside the workspace are packed as image parts. Only PNG: the host capture, the
judge and the blank measurement (hermit.runtime.pngstats) all work on PNG; a JPEG is reported, not shown.

Commands and learned tools run through the confined runner (confine.run) the agent binds; the harness owns the per-call
directory `<run>/cmd-NN/` (its `tmp` is the sandbox's /tmp, deleted after the image scan; `trace` is kept; `index.html`
is the page as it was when images were packed). Every call is recorded in `<run>/tools/NN.json`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import time
from typing import Callable

from jsonschema import Draft202012Validator

from hermit.runtime import pngstats

MAX_TOOL_CALLS = 24
MAX_ADAPTER_CALLS = 6                 # why: each image-bearing call costs ~0.7 M base64 chars of request payload
MAX_FILE_BYTES = 500_000
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
MAX_READ_BYTES = 8_000_000            # why: read_file numbers lines in memory; nothing legitimate is larger
MAX_IMAGE_BYTES = 2_000_000
MAX_IMAGE_PIXELS = 2_100_000          # why: the host capture is 1280x800; a phone capture 390x844; both well below
MAX_IMAGES_PER_CALL = 5
MAX_IMAGES_PER_RUN = 40
IMAGE_BATCH_CAP = 8_000_000           # base64 characters per image batch sent to the model (as utils/harness.py)
STDOUT_CHARS, STDERR_CHARS = 6_000, 2_000     # of a command's output shown to the model; the record keeps it all
RECORD_TEXT_CHARS = 1_000_000
RESULT_CHARS = 32_000                 # one rendered tool message (as utils/harness.py)
TOOL_TEXT_CAP = 120_000               # cumulative tool text per run; afterwards only TAIL_CHARS tails
TAIL_CHARS = 500
MIN_SECONDS_LEFT = 60                 # why: a command and the final capture both need time; refuse late calls
TIMEOUT_MARGIN = 20                   # why: the kill, the scan and the final capture need this before the deadline
DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 60, 90
READ_LINES_DEFAULT, READ_LINES_MAX = 300, 500
MAX_SCAN_FILES, SCAN_DEPTH = 4000, 8   # why: a command may unpack anything; the scan stays bounded
WM_NAMES = frozenset({"list", "memory", "recall", "search", "read", "trace"})
PRIMITIVES = ("write_file", "read_file", "replace_text", "run_command")
RESERVED = frozenset({*PRIMITIVES, "inspect_page", "wm", "tool_catalog", "tool_call", "context_package"})
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "description", "enum", "default",
                         "items", "minimum", "maximum"})
MAX_DESCRIPTION = 1024
PNG_MAGIC, JPEG_MAGIC = b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"
MIME = {"png": "image/png", "jpeg": "image/jpeg"}


def sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def function(name: str, description: str, properties: dict, required=()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required),
                           "additionalProperties": False}}}


# The descriptions name no renderer, browser or screenshot procedure: the agent is told only what the tools do.
TOOLS = [
    function("write_file", "Write a whole file inside your workspace (parent directories are created). index.html is the "
             "only file that is submitted; other files are yours to use. At most 500,000 bytes per file.",
             {"path": {"type": "string", "minLength": 1}, "content": {"type": "string", "maxLength": MAX_FILE_BYTES}},
             ["path", "content"]),
    function("read_file", "Read a text file in your workspace (README.md holds the task). Returns numbered lines from "
             "start_line (default 1), at most max_lines (default 300, maximum 500).",
             {"path": {"type": "string", "minLength": 1}, "start_line": {"type": "integer", "minimum": 1},
              "max_lines": {"type": "integer", "minimum": 1, "maximum": READ_LINES_MAX}}, ["path"]),
    function("replace_text", "Replace exactly one occurrence of old with new in a workspace file. Fails if old is absent "
             "or appears more than once.",
             {"path": {"type": "string", "minLength": 1}, "old": {"type": "string", "minLength": 1},
              "new": {"type": "string"}}, ["path", "old", "new"]),
    function("run_command", "Run one shell command (bash -c) with your workspace as the working directory, no network, "
             "a private /tmp that is discarded afterwards, and a timeout of timeout_s seconds (default 60, maximum 90). "
             "Returns exit code, stdout and stderr. PNG files the command creates or modifies inside the workspace "
             "are shown to you as images in the next message (at most five per command, forty per run).",
             {"command": {"type": "string", "minLength": 1}, "timeout_s": {"type": "integer", "minimum": 1,
                                                                            "maximum": MAX_TIMEOUT_S}}, ["command"]),
]


# --- workspace paths ----------------------------------------------------------------------------------------------------
def inside(workspace: Path, given: str) -> tuple[Path, str]:
    """(absolute path, workspace-relative posix path) for a file name the model gives: relative to the workspace or
    absolute under it, no '..', no symlink in any existing component. The leaf may not exist yet."""
    if not isinstance(given, str) or not given.strip():
        raise ValueError("path must be a non-empty string")
    workspace = Path(workspace)
    real_ws = Path(os.path.realpath(workspace))
    candidate = Path(os.path.normpath(given))
    if candidate.is_absolute():
        for root in (workspace, real_ws):
            try:
                candidate = candidate.relative_to(root)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"{given!r} lies outside the workspace")
    parts = [p for p in candidate.parts if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"{given!r} lies outside the workspace")
    node = workspace
    for part in parts:
        node = node / part
        if node.is_symlink():
            raise ValueError(f"{given!r} goes through a symbolic link, which is not admitted")
    return node, Path(*parts).as_posix()


def walk(workspace: Path):
    """(relative posix path, DirEntry) for every regular file under the workspace; symlinks are never followed."""
    count = 0
    stack = [(Path(workspace), 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < SCAN_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                return
            yield Path(entry.path).relative_to(workspace).as_posix(), entry


def workspace_bytes(workspace: Path) -> int:
    total = 0
    for _, entry in walk(workspace):
        try:
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def prune_symlinks(workspace: Path) -> list[str]:
    """Delete every symbolic link under the workspace (a command may have planted one); their relative paths."""
    removed = []
    stack = [Path(workspace)]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    os.unlink(entry.path)
                    removed.append(Path(entry.path).relative_to(workspace).as_posix())
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError:
                continue
    return sorted(removed)


# --- images ---------------------------------------------------------------------------------------------------------------
def image_kind(path: Path) -> str | None:
    try:
        with open(path, "rb") as stream:
            head = stream.read(8)
    except OSError:
        return None
    if head.startswith(PNG_MAGIC):
        return "png"
    if head.startswith(JPEG_MAGIC):
        return "jpeg"
    return None


def image_dimensions(path: Path, kind: str) -> tuple[int, int] | None:
    """(width, height) from the PNG IHDR or the first JPEG SOF marker, without decoding the pixels."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if kind == "png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    pos = 2
    while pos + 9 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return width, height
        pos += 2 + length
    return None


def scan_images(workspace: Path) -> dict[str, tuple[int, int, str]]:
    """{relative path: (mtime_ns, size, sha256)} of every PNG/JPEG regular file under the workspace now."""
    found = {}
    for rel, entry in walk(workspace):
        path = Path(entry.path)
        kind = image_kind(path)
        if kind is None:
            continue
        try:
            info = entry.stat(follow_symlinks=False)
            found[rel] = (info.st_mtime_ns, info.st_size, sha(path))
        except OSError:
            continue
    return found


def changed_images(before: dict, after: dict) -> list[str]:
    """Relative paths created or modified between two scans, oldest first (then by name)."""
    changed = [rel for rel, mark in after.items() if before.get(rel) != mark]
    return sorted(changed, key=lambda rel: (after[rel][0], rel))


def describe_image(workspace: Path, rel: str) -> tuple[dict | None, str | None]:
    """(record, None) for an admissible image at workspace/rel — PNG magic, a regular file reached without a symlink,
    <= MAX_IMAGE_BYTES, <= MAX_IMAGE_PIXELS — else (None, problem); a JPEG is found by the scan and rejected with a
    reason, so the model learns why it saw nothing. `nonblank` comes from pngstats."""
    try:
        path, rel = inside(workspace, rel)
    except ValueError as exc:
        return None, str(exc)
    try:
        info = os.lstat(path)
    except OSError:
        return None, "does not exist"
    if not stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    if info.st_size > MAX_IMAGE_BYTES:
        return None, f"{info.st_size} bytes, more than {MAX_IMAGE_BYTES}"
    kind = image_kind(path)
    if kind is None:
        return None, "not a PNG file"
    if kind != "png":
        return None, "a JPEG file: only PNG images are shown"
    dims = image_dimensions(path, kind)
    if dims is None:
        return None, "image header unreadable"
    width, height = dims
    if width * height > MAX_IMAGE_PIXELS:
        return None, f"{width}x{height} is more than {MAX_IMAGE_PIXELS} pixels"
    record = {"file": rel, "kind": kind, "sha256": sha(path), "bytes": info.st_size, "width": width, "height": height,
              "nonblank": None, "stats": None, "packed_for_model": False}
    if kind == "png":
        measured = pngstats.stats(path)
        record["nonblank"] = not measured["blank"]
        record["stats"] = {k: measured[k] for k in ("mean_luma", "nonuniform_fraction", "dominant_rgb", "error")}
    return record, None


def pack_images(paths: list[Path], cap: int = IMAGE_BATCH_CAP, labels: list[str] | None = None) -> tuple[list, list]:
    """Image parts for one user message (a text label then an image_url per file, fixed order) and a packing record per
    path: {file, base64_characters, packed_for_model: True} or {file, omitted: reason}. A file that would push the batch
    over `cap` base64 characters is omitted, the next smaller one may still fit (as utils/harness.images_for_model)."""
    parts, records, total = [], [], 0
    for index, path in enumerate(paths):
        path = Path(path)
        label = labels[index] if labels else path.name
        kind = image_kind(path) or "png"
        encoded = base64.b64encode(path.read_bytes()).decode()
        if total + len(encoded) > cap:
            records.append({"file": label, "omitted": f"{cap:,} base64-character batch cap", "packed_for_model": False})
            continue
        total += len(encoded)
        parts += [{"type": "text", "text": f"{label} — image file written in your workspace"},
                  {"type": "image_url", "image_url": {"url": f"data:{MIME[kind]};base64,{encoded}"}}]
        records.append({"file": label, "base64_characters": len(encoded), "packed_for_model": True})
    return parts, records


# --- the adapter's learned tools --------------------------------------------------------------------------------------
def sanitise_schema(schema: object) -> dict:
    """A parameters schema reduced to the keywords a function tool may carry: type object, allowed keywords only,
    additionalProperties false, `required` limited to declared properties."""
    if not isinstance(schema, dict):
        schema = {}
    out = {k: v for k, v in schema.items() if k in SCHEMA_KEYS}
    out["type"] = "object"
    properties = out.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    out["properties"] = {str(k): _sanitise_property(v) for k, v in properties.items()}
    required = out.get("required")
    if not isinstance(required, list):
        required = []
    out["required"] = [r for r in required if isinstance(r, str) and r in out["properties"]]
    out["additionalProperties"] = False
    return out


def _sanitise_property(schema: object) -> dict:
    if not isinstance(schema, dict):
        return {}
    out = {k: v for k, v in schema.items() if k in SCHEMA_KEYS}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {str(k): _sanitise_property(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _sanitise_property(out["items"])
    return out


def expose_catalog(catalog: dict) -> tuple[list[dict], list[dict]]:
    """Function tools for the admitted entries of a `tool_catalog` result, and the entries skipped [{name, problem}]:
    a name that is not a slug, collides with a primitive or a reserved name, or repeats; a description that is empty."""
    specs, skipped, seen = [], [], set()
    for entry in (catalog or {}).get("tools") or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            skipped.append({"name": str(name), "problem": "not a tool name (^[a-z][a-z0-9_]{0,63}$)"})
            continue
        if name in RESERVED:
            skipped.append({"name": name, "problem": "collides with a primitive or reserved name"})
            continue
        if name in seen:
            skipped.append({"name": name, "problem": "duplicate name"})
            continue
        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            skipped.append({"name": name, "problem": "no description"})
            continue
        seen.add(name)
        specs.append({"type": "function", "function": {"name": name, "description": description[:MAX_DESCRIPTION],
                                                       "parameters": sanitise_schema(entry.get("parameters"))}})
    return specs, skipped


class ConfinedRunner:
    """The `tool_runner` resource bound into the Session: confine.run with the harness's per-call tmp directory, trace
    file and time cap injected, and the raw record kept (`last`) for the call's tools/NN.json."""

    def __init__(self, run: Callable):
        self.run = run
        self.tmp_dir: str | None = None
        self.trace: str | None = None
        self.timeout_cap: float | None = None
        self.last: dict | None = None

    def __call__(self, argv, cwd, timeout_s, **kw) -> dict:
        if self.tmp_dir is not None:
            kw.setdefault("tmp_dir", self.tmp_dir)
        if self.trace is not None:
            kw.setdefault("trace", self.trace)
        if self.timeout_cap is not None:
            timeout_s = max(1.0, min(float(timeout_s), self.timeout_cap))
        self.last = self.run(argv=argv, cwd=cwd, timeout_s=timeout_s, **kw)
        return self.last


def _names_other_page(name: str, args) -> bool:
    """True when a learned tool's arguments name an .html file other than index.html: its pictures then show that
    file, so the call is not a look at index.html (a seen version is keyed by index.html at the time of the call)."""
    if name in PRIMITIVES or not isinstance(args, dict):
        return False
    return any(isinstance(v, str) and v.lower().endswith((".html", ".htm")) and Path(v).name != "index.html"
               for v in args.values())


# --- the harness ----------------------------------------------------------------------------------------------------------
class Harness:
    """One run's tools over `workspace`; records go under `run` (tools/NN.json, cmd-NN/).

    `runner` is a ConfinedRunner; `session` (arm C) a hermit Session whose `tool_call` runs the `adapter_tools`
    (function specs from expose_catalog) and whose `wm` (when `wm_spec` is given) answers read-only knowledge calls."""

    def __init__(self, workspace, run, case, *, runner: ConfinedRunner, session=None, adapter_tools=(), wm_spec=None):
        self.workspace, self.run, self.case = Path(workspace), Path(run), case
        self.runner, self.session = runner, session
        self.adapter_names = [t["function"]["name"] for t in adapter_tools]
        self.tools = [*TOOLS, *adapter_tools]
        if wm_spec is not None:
            self.tools.append(wm_spec)
        self.schemas = {t["function"]["name"]: t["function"]["parameters"] for t in self.tools}
        self.calls = self.run_commands = self.adapter_calls = self.wm_calls = self.tool_errors = 0
        self.images_total = self.text_chars = 0
        self.image_bearing_calls = self.looked_calls = self.looked_blank_calls = 0
        self.first_image_call = self.first_look_call = None
        self.last_look_index_sha = None
        self.last_dispatch_look: dict | None = None      # the latest call's look ({call, sha, snapshot}) or None
        self.tools_called: dict[str, int] = {}
        self.summaries: list[dict] = []

    # --- dispatch ---------------------------------------------------------------------------------------------------
    def dispatch(self, name: str, args, deadline: float) -> tuple[str, list]:
        """Run one requested tool call (every request counts); returns the tool message text and new image parts."""
        self.calls += 1
        n = self.calls
        start = time.monotonic()
        self.tools_called[name] = self.tools_called.get(name, 0) + 1
        record = {"name": name, "arguments": args, "result": None, "seconds": None, "lock_wait_s": None,
                  "timed_out": False, "confined": False, "bwrap_argv_sha": None, "stdout_bytes": None,
                  "stderr_bytes": None, "dropped_bytes": None, "image_records": [], "images_rejected": [],
                  "browser_launched": None, "index_sha_at_call": None, "symlinks_removed": []}
        parts: list = []
        try:
            if n > MAX_TOOL_CALLS:
                raise ValueError("Tool call budget exhausted")
            if name not in self.schemas:
                raise ValueError("Unknown tool")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be a JSON object")
            Draft202012Validator(self.schemas[name]).validate(args)
            if time.monotonic() >= deadline:
                raise TimeoutError("Actor time budget exhausted")
            if name == "write_file":
                result = self._write_file(args)
            elif name == "read_file":
                result = self._read_file(args)
            elif name == "replace_text":
                result = self._replace_text(args)
            elif name == "run_command":
                result, parts = self._run_command(n, args, deadline, record)
            elif name == "wm":
                result = self._wm(args)
            else:
                result, parts = self._adapter_tool(n, name, args, deadline, record)
        except Exception as exc:  # noqa: BLE001 - every failure is text for the model, never an exception
            self.tool_errors += 1
            result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:2000]}"}
        record.update(result=result, seconds=round(time.monotonic() - start, 3))
        record["result"] = self._bounded(result)
        save(self.run / "tools" / f"{n:02d}.json", record)
        exit_code = result.get("exit_code") if isinstance(result, dict) else None
        nonblank = any(r.get("nonblank") for r in record["image_records"])
        looked = bool(parts) and bool(record["browser_launched"]) and nonblank
        self.last_dispatch_look = None
        if parts and record["browser_launched"]:       # a look: the browser ran and its pictures reached the model
            if looked:
                self.looked_calls += 1
                self.first_look_call = self.first_look_call or n
                self.last_look_index_sha = record["index_sha_at_call"]
                if record["index_sha_at_call"] and not _names_other_page(name, args):
                    self.last_dispatch_look = {"call": n, "sha": record["index_sha_at_call"],
                                               "snapshot": str(self.run / f"cmd-{n:02d}" / "index.html")}
            else:
                self.looked_blank_calls += 1
        self.summaries.append({"call": n, "name": name, "exit_code": exit_code, "images": sum(
            1 for r in record["image_records"] if r.get("packed_for_model")), "looked": looked})
        return self._render(self._for_model(result)), parts

    # --- file tools -------------------------------------------------------------------------------------------------
    def _write_file(self, args: dict) -> dict:
        path, rel = inside(self.workspace, args["path"])
        data = args["content"].encode()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("File byte limit exceeded")
        if path.is_dir():
            raise ValueError(f"{rel} is a directory")
        existing = path.stat().st_size if path.is_file() else 0
        if workspace_bytes(self.workspace) - existing + len(data) > MAX_WORKSPACE_BYTES:
            raise ValueError("Workspace size limit exceeded")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"ok": True, "file": rel, "bytes": len(data), "sha256": sha(path)}

    def _read_file(self, args: dict) -> dict:
        path, rel = inside(self.workspace, args["path"])
        if not path.is_file():
            raise ValueError(f"{rel} is not a file")
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError(f"{rel} is too large to read as text")
        data = path.read_bytes()
        if b"\0" in data:
            raise ValueError(f"{rel} is not a text file")
        lines = data.decode("utf-8", "replace").splitlines()
        start_line = args.get("start_line", 1)
        limit = args.get("max_lines", READ_LINES_DEFAULT)
        return {"ok": True, "file": rel, "total_lines": len(lines), "text": "\n".join(
            f"{i + start_line}: {v}" for i, v in enumerate(lines[start_line - 1:start_line - 1 + limit]))}

    def _replace_text(self, args: dict) -> dict:
        path, rel = inside(self.workspace, args["path"])
        if not path.is_file():
            raise ValueError(f"{rel} is not a file")
        text = path.read_text(errors="replace")
        count = text.count(args["old"])
        if count != 1:
            raise ValueError(f"old must occur exactly once, found {count}")
        text = text.replace(args["old"], args["new"])
        if len(text.encode()) > MAX_FILE_BYTES:
            raise ValueError("File byte limit exceeded")
        path.write_text(text)
        return {"ok": True, "file": rel, "sha256": sha(path)}

    # --- confined tools ---------------------------------------------------------------------------------------------
    def _seconds_left(self, deadline: float, what: str) -> float:
        left = deadline - time.monotonic()
        if left < MIN_SECONDS_LEFT:
            raise TimeoutError(f"Less than {MIN_SECONDS_LEFT} seconds remain; no further {what}")
        return left

    def _call_dir(self, n: int) -> tuple[Path, Path, Path]:
        directory = self.run / f"cmd-{n:02d}"
        tmp, trace = directory / "tmp", directory / "trace"
        tmp.mkdir(parents=True, exist_ok=True)
        return directory, tmp, trace

    def _run_command(self, n: int, args: dict, deadline: float, record: dict) -> tuple[dict, list]:
        left = self._seconds_left(deadline, "commands")
        timeout = max(1.0, min(float(args.get("timeout_s", DEFAULT_TIMEOUT_S)), MAX_TIMEOUT_S, left - TIMEOUT_MARGIN))
        directory, tmp, trace = self._call_dir(n)
        self.run_commands += 1
        prune_symlinks(self.workspace)
        before = scan_images(self.workspace)
        raw = self.runner(argv=["bash", "-c", args["command"]], cwd=str(self.workspace), timeout_s=timeout,
                          lock="shared", tmp_dir=str(tmp), trace=str(trace))
        raw = raw if isinstance(raw, dict) else {"error": "the runner returned no record"}
        record["symlinks_removed"] = prune_symlinks(self.workspace)
        candidates = changed_images(before, scan_images(self.workspace))
        parts = self._admit_images(n, directory, tmp, candidates, record)
        stdout, stderr = str(raw.get("stdout") or ""), str(raw.get("stderr") or "")
        self._note_run(record, raw)
        result = {"exit_code": raw.get("exit_code"), "stdout": stdout[:RECORD_TEXT_CHARS],
                  "stderr": stderr[:RECORD_TEXT_CHARS], "seconds": raw.get("seconds"),
                  "timed_out": bool(raw.get("timed_out")), "lock_wait_s": raw.get("lock_wait_s"),
                  "error": raw.get("error"), "timeout_s": timeout,
                  "images": [self._image_for_model(r) for r in record["image_records"]],
                  "images_rejected": record["images_rejected"]}
        return result, parts

    def _adapter_tool(self, n: int, name: str, args: dict, deadline: float, record: dict) -> tuple[dict, list]:
        if self.adapter_calls >= MAX_ADAPTER_CALLS:
            raise ValueError("adapter tool budget exhausted")
        left = self._seconds_left(deadline, "adapter tool calls")
        directory, tmp, trace = self._call_dir(n)
        self.adapter_calls += 1
        prune_symlinks(self.workspace)
        self.runner.tmp_dir, self.runner.trace = str(tmp), str(trace)
        self.runner.timeout_cap, self.runner.last = max(1.0, left - TIMEOUT_MARGIN), None
        try:
            reply = self.session.call("tool_call", {"name": name, "args": args})
        finally:
            self.runner.tmp_dir = self.runner.trace = self.runner.timeout_cap = None
        raw = self.runner.last or {}
        record["symlinks_removed"] = prune_symlinks(self.workspace)
        reply = reply if isinstance(reply, dict) else {"text": json.dumps(reply), "images": []}
        listed = [p for p in (reply.get("images") or []) if isinstance(p, str)]
        parts = self._admit_images(n, directory, tmp, listed, record)
        record["images_rejected"] = [*(reply.get("images_rejected") or []), *record["images_rejected"]]
        self._note_run(record, raw)
        if record["browser_launched"] is None:
            record["browser_launched"] = reply.get("browser_launched")
        result = {"text": reply.get("text", ""), "exit_code": reply.get("exit_code"), "seconds": reply.get("seconds"),
                  "timed_out": bool(reply.get("timed_out")), "lock_wait_s": reply.get("lock_wait_s"),
                  "error": reply.get("error"), "stderr_tail": str(reply.get("stderr_tail") or "")[-STDERR_CHARS:],
                  "images": [self._image_for_model(r) for r in record["image_records"]],
                  "images_rejected": record["images_rejected"]}
        return result, parts

    def _wm(self, args: dict) -> dict:
        self.wm_calls += 1
        if args.get("name") not in WM_NAMES:
            raise ValueError("Only read-only knowledge operations are admitted; no executable tools or delegates")
        return self.session.call("wm", args)

    def _note_run(self, record: dict, raw: dict) -> None:
        record.update(confined=True, lock_wait_s=raw.get("lock_wait_s"), timed_out=bool(raw.get("timed_out")),
                      bwrap_argv_sha=raw.get("bwrap_argv_sha"), dropped_bytes=raw.get("dropped_bytes"),
                      stdout_bytes=len(str(raw.get("stdout") or "").encode()),
                      stderr_bytes=len(str(raw.get("stderr") or "").encode()),
                      browser_launched=raw.get("browser_launched"))

    def _admit_images(self, n: int, directory: Path, tmp: Path, candidates: list[str], record: dict) -> list:
        """Validate the candidate images (per-call and per-run caps), pack the admitted ones, update the look counters,
        delete the call's tmp directory and keep an index.html snapshot when images were packed."""
        accepted: list[dict] = []
        for rel in candidates:
            if len(accepted) >= MAX_IMAGES_PER_CALL:
                record["images_rejected"].append({"file": rel,
                                                  "problem": f"more than {MAX_IMAGES_PER_CALL} images in one call"})
                continue
            if self.images_total >= MAX_IMAGES_PER_RUN:
                record["images_rejected"].append({"file": rel, "problem": "image budget exhausted"})
                continue
            image, problem = describe_image(self.workspace, rel)
            if problem:
                record["images_rejected"].append({"file": rel, "problem": problem})
                continue
            if any(a["file"] == image["file"] for a in accepted):
                continue
            self.images_total += 1
            accepted.append(image)
        parts, packing = pack_images([self.workspace / a["file"] for a in accepted], IMAGE_BATCH_CAP,
                                     [a["file"] for a in accepted])
        for image, packed in zip(accepted, packing):
            image.update(packed_for_model=packed["packed_for_model"],
                         base64_characters=packed.get("base64_characters"), omitted=packed.get("omitted"))
        record["image_records"] = accepted
        shutil.rmtree(tmp, ignore_errors=True)
        if parts:
            index = self.workspace / "index.html"
            if index.is_file() and not index.is_symlink():
                shutil.copyfile(index, directory / "index.html")
                record["index_sha_at_call"] = sha(index)
            self.image_bearing_calls += 1
            if self.first_image_call is None:
                self.first_image_call = n
        return parts

    # --- rendering --------------------------------------------------------------------------------------------------
    @staticmethod
    def _image_for_model(image: dict) -> dict:
        out = {"file": image["file"], "bytes": image["bytes"], "width": image["width"], "height": image["height"],
               "packed_for_model": image["packed_for_model"]}
        if image.get("omitted"):
            out["omitted"] = image["omitted"]
        return out

    @staticmethod
    def _for_model(result) -> dict:
        if not isinstance(result, dict):
            return {"result": result}
        out = dict(result)
        if isinstance(out.get("stdout"), str) and len(out["stdout"]) > STDOUT_CHARS:
            out["stdout"] = out["stdout"][:STDOUT_CHARS] + f"\n[... clipped to {STDOUT_CHARS} characters]"
        if isinstance(out.get("stderr"), str) and len(out["stderr"]) > STDERR_CHARS:
            out["stderr"] = out["stderr"][-STDERR_CHARS:]
        return out

    @staticmethod
    def _bounded(result) -> dict:
        if not isinstance(result, dict):
            return {"result": result}
        return {k: (v[:RECORD_TEXT_CHARS] if isinstance(v, str) else v) for k, v in result.items()}

    def _render(self, result: dict) -> str:
        rendered = json.dumps(result, ensure_ascii=False)
        if len(rendered) > RESULT_CHARS:
            rendered = json.dumps({"truncated": True, "original_characters": len(rendered),
                                   "prefix": rendered[:RESULT_CHARS - 2000]})
        if self.text_chars + len(rendered) > TOOL_TEXT_CAP:
            rendered = json.dumps({"truncated": True, "tail": rendered[-TAIL_CHARS:],
                                   "reason": f"cumulative tool text cap of {TOOL_TEXT_CAP:,} characters reached"},
                                  ensure_ascii=False)
        self.text_chars += len(rendered)
        return rendered
