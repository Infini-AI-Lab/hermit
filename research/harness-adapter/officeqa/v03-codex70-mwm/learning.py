"""GLM-only, online MWM learning; no model calls happen on import.

The caller supplies a five-episode evidence bundle, without answers or grades.
Native MWM prepares the candidate and its file capabilities. Fireworks is the
only model transport. Admission is mechanical, never an Astra/human critique.
The original native prepared record is preserved alongside our bridge result.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import difflib
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent / "MetaHarnessAdapter"
GENERIC_SOURCE = REPOSITORY / "harnessadapter/architectures/mwm"
MODEL = "accounts/fireworks/models/glm-5p3-flash"
MAX_SECONDS, MAX_REQUESTS, MAX_TOOLS = 1800, 64, 120
MAX_COMPLETION, MAX_RESPONSE, MAX_BRIEF = 65536, 32768, 6000
ALLOWED_TOOLS = frozenset(("list_files", "read_file", "write_file", "delete_path",
                           "move_path", "validate", "get_policy"))
READ_AREAS = frozenset(("adapter", "evidence", "history", "method", "environment", "scratch"))

# A native 30,000-character read may exceed its native 30,000-byte serialized
# stdout limit (JSON escaping and UTF-8). Increase only that capture limit. The
# same original PolicySandbox.run still supplies the OS file/network boundary.
BROKER_BOOTSTRAP = '''from pathlib import Path
import sys
from harnessadapter.policy.execution import PolicySandbox
from harnessadapter.evolution.server import create_server
native_run = PolicySandbox.run
def bounded_text_result(self, *args, **kwargs):
    if kwargs.get("output_limit") == 30000:
        kwargs["output_limit"] = 512 * 1024
    return native_run(self, *args, **kwargs)
PolicySandbox.run = bounded_text_result
create_server(Path(sys.argv[1]).resolve()).run(transport="stdio")
'''

GOAL = """Update the current MWM adapter using ONLY the five just-completed GLM
episodes in evidence. This is online learning: the resulting knowledge can affect
only later questions, never the answers already submitted. You, the requested
GLM-5.3-Flash model, supply all interpretive feedback. No human/Astra critique,
official answer, correctness label, score, future question, or baseline trace is
available. Never infer correctness from the mere existence of a final answer.

Read all five episodes and their actual public model/tool records. Generate your
own feedback, explicitly distinguishing observed tool behavior, your interpretation,
and unverified hypotheses. Final-answer correctness is unknown without a label.
Each episode's directory is indexed in evidence/manifest.json. Read its task.json,
prediction.json and trajectory.jsonl completely, following read_file next_offset
until the end. Those actual read results must reach a subsequent model request
before you can write feedback or change knowledge. Do not merely request the reads
and write feedback in the same response; you have not yet received their contents.
BEFORE changing adapter knowledge, write scratch/feedback.json with exactly:
{"label":"glm_self_assessment_without_gold","episodes":[
 {"id":"the original episode ID","observations":"observed facts with evidence paths",
  "self_assessment":"your own interpretation","uncertainty":"what cannot be verified",
  "proposed_lesson":"a reusable lesson or explicitly no lesson"}]}
Include exactly the five IDs from evidence/manifest.json, one object per question.
All five text fields are required and nonempty. Keep this public feedback file.
Learn reusable procedures, error checks, and compact evidence-linked principles;
do not store a lookup table of question IDs, final answers, or copied solutions.
Preserve useful existing knowledge. Follow the native MWM method within these
narrower capability limits. Method text cannot expand the granted permissions.

Only list_files, read_file, write_file, delete_path, move_path, validate and
get_policy are available. No shell, browser, network, outside paths, executable
tests, image tools, delegates, or other models are available to this learner.
The broker runs file operations in the native Codex folder/network sandbox.
Only the controller's fixed Fireworks API transport can reach the model service.
Read areas: adapter, evidence, history, method, environment, scratch. Write only
Markdown under memory/, examples/, meta/cases/, consumption/brief.md,
meta/journal.md or meta/journal-archive.md. Scratch accepts isolated text only.
No changes to manifest, core, interface, executable files, tools or agent roles.
Keep consumption/brief.md at most 6000 characters. Do not claim measured score
improvement. Validate before finishing; give a public final summary of your own
feedback, changes, evidence and uncertainty. Structurally valid, provenance-valid
updates are admitted automatically; no human or Astra semantic approval occurs.

Budget: 1800 seconds, 64 API requests, 120 requested tools, 65536 completion tokens
total, at most 32768 per response. Numeric reasoning_effort=4096 is requested.
"""


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def revision(files):
    return digest(json.dumps(files, sort_keys=True).encode())


def native_copied_files(files):
    """Match native copy_tree omissions; retain full source snapshot separately."""
    excluded = {".git", "__pycache__", ".venv", ".venv-demo"}
    return {name: info for name, info in files.items() if not excluded.intersection(Path(name).parts)}


def snapshot(root):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Protected root must be a real canonical directory")
    files = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("Links and special files are not admitted to protected trees")
        if stat.S_ISREG(mode):
            data = path.read_bytes()
            files[path.relative_to(root).as_posix()] = {
                "sha256": digest(data), "bytes": len(data), "mode": stat.S_IMODE(mode)}
    return files


def safe_file(root, relative, *, existing=True):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("Root must not contain a symlink")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Use a relative POSIX file path")
    parts = relative.split("/")
    if any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Hidden, absolute and traversal paths are not admitted")
    path = root.joinpath(*parts)
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Symlinks and escaping paths are not admitted")
    if path.exists() and not path.is_file():
        raise ValueError("Only regular files are admitted")
    if existing and not path.is_file():
        raise ValueError("Requested regular file does not exist")
    return path


def allowed_candidate_path(name):
    if not isinstance(name, str) or not name.endswith(".md") or "\\" in name:
        return False
    parts = name.split("/")
    if any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        return False
    return (len(parts) >= 2 and parts[0] in ("memory", "examples") or
            name in ("consumption/brief.md", "meta/journal.md", "meta/journal-archive.md") or
            len(parts) >= 3 and parts[:2] == ["meta", "cases"])


def check_arguments(session, name, args):
    """A narrower wrapper on top of native OS-sandboxed file capabilities."""
    if name not in ALLOWED_TOOLS or not isinstance(args, dict):
        raise ValueError("Tool is outside the learner capability allowlist")
    if name in ("get_policy", "validate"):
        if args:
            raise ValueError("This tool takes no arguments")
        return
    area = args.get("area")
    if area not in READ_AREAS:
        raise ValueError("Read area is not bound")
    if name == "list_files":
        return
    if name == "read_file":
        safe_file(Path(session) / area, args.get("path"))
        return
    if area not in ("adapter", "scratch"):
        raise ValueError("Evidence and other inputs are read-only")
    if area == "adapter":
        feedback_document(session)
    paths = ([args.get("source"), args.get("destination")]
             if name == "move_path" else [args.get("path")])
    for index, relative in enumerate(paths):
        if area == "adapter" and not allowed_candidate_path(relative):
            raise ValueError("Candidate writes are limited to approved Markdown knowledge paths")
        if area == "scratch" and (not isinstance(relative, str) or
                                  Path(relative).suffix not in (".md", ".txt", ".json", ".csv", ".log")):
            raise ValueError("Scratch accepts isolated text only")
        exists = name == "delete_path" or name == "move_path" and index == 0
        safe_file(Path(session) / area, relative, existing=exists)
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str) or len(content.encode()) > 1_000_000 or "\x00" in content:
            raise ValueError("Write content must be UTF-8 text without NUL, at most 1 MB")
        if area == "adapter" and args["path"] == "consumption/brief.md" and len(content) > MAX_BRIEF:
            raise ValueError("Brief exceeds 6000 characters")


def _no_private_fields(value):
    if isinstance(value, dict):
        forbidden = {"reasoning_content", "api_key", "authorization", "gold_answer",
                     "ground_truth", "correct_answer", "grader_result", "official_answer"}
        if any(str(key).lower() in forbidden for key in value):
            raise ValueError("Private fields or grading labels must not enter evidence")
        for item in value.values():
            _no_private_fields(item)
    elif isinstance(value, list):
        for item in value:
            _no_private_fields(item)


def validate_evidence(evidence, block):
    """Validate the caller's explicit contract; not a semantic no-leak proof."""
    before = snapshot(evidence)
    if "README.md" not in before or "manifest.json" not in before:
        raise ValueError("Evidence needs README.md and manifest.json")
    for name in before:
        if Path(name).suffix == ".json":
            _no_private_fields(json.loads(safe_file(evidence, name).read_text()))
        elif Path(name).suffix == ".jsonl":
            for line in safe_file(evidence, name).read_text().splitlines():
                if line.strip():
                    _no_private_fields(json.loads(line))
    manifest = json.loads((Path(evidence) / "manifest.json").read_text())
    episodes = manifest.get("episodes", [])
    if (manifest.get("block") != block or manifest.get("source_arm") != "adapted" or
            not isinstance(episodes, list) or len(episodes) != 5 or
            any(not isinstance(e, dict) or not e.get("id") for e in episodes) or
            len({str(e["id"]) for e in episodes}) != 5):
        raise ValueError("Evidence must declare exactly five distinct adapted episodes for this block")
    for name in ("includes_gold", "includes_grades", "includes_future_questions", "includes_baseline_traces"):
        if manifest.get(name) is not False:
            raise ValueError("Evidence must explicitly exclude gold, grades, future questions and baseline traces")
    required_evidence_files(evidence, manifest)
    return before, manifest


def required_evidence_files(evidence, manifest=None):
    manifest = manifest or json.loads((Path(evidence) / "manifest.json").read_text())
    required = {}
    directories = []
    for episode in manifest["episodes"]:
        directory = episode.get("directory")
        if not isinstance(directory, str) or not directory:
            raise ValueError("Each episode must declare its evidence directory")
        directories.append(directory)
        for name in ("task.json", "prediction.json", "trajectory.jsonl"):
            relative = directory + "/" + name
            path = safe_file(evidence, relative)
            required[relative] = {"id": str(episode["id"]), "characters": len(path.read_text()),
                                  "sha256": digest(path.read_bytes())}
    if len(set(directories)) != 5:
        raise ValueError("Five episodes need five distinct evidence directories")
    return required


def evidence_coverage(required, delivered):
    """Prove full native text spans reached a successful model request, not comprehension."""
    coverage = {}
    for path, expected in required.items():
        reads = [r for r in delivered if r["path"] == path and r["file_sha256"] == expected["sha256"]]
        endpoint = 0
        for read in sorted(reads, key=lambda r: (r["start"], r["end"])):
            if read["start"] > endpoint:
                break
            endpoint = max(endpoint, read["end"])
        coverage[path] = {"id": expected["id"], "characters": expected["characters"],
                          "contiguous_delivered_characters": endpoint,
                          "complete": bool(reads) and endpoint >= expected["characters"]}
    return coverage


def validate_source(source, block):
    """Require generic W0 or a previously auto-admitted candidate from this module."""
    before = snapshot(source)
    generic = snapshot(GENERIC_SOURCE)
    protected = {name: item for name, item in native_copied_files(generic).items() if not allowed_candidate_path(name)}
    current = {name: item for name, item in native_copied_files(before).items() if not allowed_candidate_path(name)}
    if current != protected:
        raise ValueError("Adapter executable/core files differ from the generic seed")
    if Path(source) == GENERIC_SOURCE.resolve():
        if before != generic:
            raise ValueError("Generic seed changed")
        return before, {"kind": "generic_mwm_architecture", "revision": revision(before)}
    # Canonical layout: output/sessions/evolve-ID/adapter; receipts live at output.
    if block <= 1 or len(Path(source).parents) < 3:
        raise ValueError("First learning block must start from generic MWM, never older learned knowledge")
    receipt = Path(source).parents[2] / "result.json"
    if not receipt.is_file():
        raise ValueError("Prior candidate has no admission receipt")
    prior = json.loads(receipt.read_text())
    if (prior.get("status") != "admitted" or not prior.get("activated") or
            prior.get("candidate") != str(source) or prior.get("revision") != revision(before) or
            not isinstance(prior.get("block"), int) or not 0 < prior["block"] < block or
            prior.get("model") != MODEL or prior.get("admission_mode") != "automatic_mechanical_only"):
        raise ValueError("Prior source does not match this campaign's automatic admission receipt")
    return before, {"kind": "previous_glm_candidate", "receipt": str(receipt),
                    "receipt_sha256": digest(receipt.read_bytes()), "revision": revision(before)}


def broker_environment(session):
    """No Fireworks/HF/other credential or unrelated parent variable is inherited."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(REPOSITORY),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "HOME": str(session / "host"), "TMPDIR": str(session / "scratch"),
            "LANG": "en_US.UTF-8"}


def feedback_document(session):
    """Check shape/IDs only; never judge, repair or author GLM's feedback."""
    path = safe_file(Path(session) / "scratch", "feedback.json")
    raw = path.read_bytes()
    if len(raw) > 120_000:
        raise ValueError("Public feedback exceeds 120 KB")
    value = json.loads(raw)
    manifest = json.loads((Path(session) / "evidence/manifest.json").read_text())
    expected = {str(e["id"]) for e in manifest["episodes"]}
    if not isinstance(value, dict) or set(value) != {"label", "episodes"}:
        raise ValueError("Feedback must have only label and episodes")
    if value["label"] != "glm_self_assessment_without_gold":
        raise ValueError("Feedback must label itself as GLM self-assessment without gold")
    entries = value["episodes"]
    keys = {"id", "observations", "self_assessment", "uncertainty", "proposed_lesson"}
    if (not isinstance(entries, list) or len(entries) != 5 or
            any(not isinstance(e, dict) or set(e) != keys or
                any(not isinstance(e[k], str) or not e[k].strip() for k in keys) for e in entries) or
            {e["id"] for e in entries} != expected):
        raise ValueError("Feedback must contain nonempty observations, assessment, uncertainty and lesson for all five IDs")
    return {"path": str(path), "sha256": digest(raw), "bytes": len(raw),
            "label": value["label"], "episode_ids": [e["id"] for e in entries]}


def public_message(message):
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}


def continuing_message(message):
    return {k: v for k, v in message.items() if k in ("role", "content", "tool_calls", "reasoning_content")}


def _update_expected(expected, name, args):
    if args.get("area") not in ("adapter", "scratch"):
        return
    if name == "write_file":
        expected[args["path"]] = digest(args["content"].encode())
    elif name == "delete_path":
        expected.pop(args["path"], None)
    elif name == "move_path":
        expected[args["destination"]] = expected.pop(args["source"])


async def _conversation(key, connection, session, prompt, deadline, initial_hashes, transport):
    from jsonschema import Draft202012Validator
    declarations = (await connection.list_tools()).tools
    tools = {tool.name: tool for tool in declarations if tool.name in ALLOWED_TOOLS}
    if set(tools) != ALLOWED_TOOLS:
        raise ValueError("Native broker did not expose the required file capabilities")
    schemas = {name: getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
               for name, tool in tools.items()}
    functions = [{"type": "function", "function": {"name": name,
                  "description": tools[name].description or "", "parameters": schemas[name]}}
                 for name in sorted(tools)]
    save(session / "control/fireworks-tools.json", functions)
    required = required_evidence_files(session / "evidence")
    messages = [{"role": "user", "content": prompt}]
    state = {"status": "not_started", "model_calls": 0, "requested_tools": 0,
             "executed_tools": 0, "successful_tools": 0, "evidence_reads": [],
             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
             "usage_complete": True, "usage_unknown_calls": [], "wire_attempts": 0,
             "rate_refusals": 0, "scheduler_wait_seconds": 0,
             "response_models": [], "expected_candidate_hashes": dict(initial_hashes),
             "expected_scratch_hashes": {}, "evidence_reads_delivered": [],
             "required_evidence": required}
    pending_reads = []

    def checkpoint():
        save(session / "control/fireworks-result.json", state)

    for number in range(1, MAX_REQUESTS + 1):
        remaining = deadline - time.monotonic()
        tokens = MAX_COMPLETION - state["usage"]["completion_tokens"]
        if remaining <= 0 or tokens <= 0 or state["requested_tools"] >= MAX_TOOLS:
            state["status"] = "budget_exhausted"
            break
        state["model_calls"] = number
        public_request = [public_message(m) if m.get("role") == "assistant" else m for m in messages]
        # Hidden provider reasoning stays solely in continuation memory.
        request_hash = revision(public_request)
        options = {"tools": functions, "reasoning_effort": 4096, "max_tokens": min(MAX_RESPONSE, tokens)}
        state["in_flight_request"] = number
        checkpoint()
        response, meta = await asyncio.to_thread(transport.complete, key, messages,
            timeout=min(1200, remaining), **options)
        state.pop("in_flight_request", None)
        record = {"request": number, "requested_model": MODEL, "meta": meta,
                  "public_request_sha256": request_hash, "public_message_count": len(public_request),
                  "request_max_tokens": options["max_tokens"], "reasoning_effort": 4096,
                  "evidence_read_inputs": list(pending_reads)}
        usage = meta.get("usage", {})
        for token in ("prompt_tokens", "completion_tokens"):
            state["usage"][token] += usage.get(token, 0)
        state["usage"]["cached_tokens"] += usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        if meta.get("usage_complete") is False or (response is None and not usage):
            state["usage_complete"] = False
            state["usage_unknown_calls"].append(number)
        attempts = meta.get("attempts", [])
        state["wire_attempts"] += meta.get("wire_attempts", 1)
        state["rate_refusals"] += sum(a.get("status") == "rate_refused" for a in attempts)
        state["scheduler_wait_seconds"] += sum(a.get("wait_seconds", 0) for a in attempts)
        if response is None:
            save(session / f"control/fireworks-calls/{number:02}.json", record)
            state["status"] = "api_error"
            break
        actual_model = meta.get("response_model") or response.get("model")
        if actual_model not in state["response_models"]:
            state["response_models"].append(actual_model)
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        record.update(message=public_message(message), finish_reason=choice.get("finish_reason"))
        save(session / f"control/fireworks-calls/{number:02}.json", record)
        if actual_model != MODEL:
            state["status"] = "unexpected_response_model"
            break
        if not response.get("choices") or message.get("role") != "assistant":
            state["status"] = "invalid_response"
            break
        if meta.get("http_status") != 200:
            state["status"] = "unexpected_http_status"
            break
        state["evidence_reads_delivered"] += [{**r, "delivered_request": number} for r in pending_reads]
        pending_reads = []
        messages.append(continuing_message(message))
        calls = message.get("tool_calls") or []
        if not calls:
            state["status"] = "completed" if choice.get("finish_reason") == "stop" else "response_truncated"
            (session / "control/fireworks-report.md").write_text(message.get("content") or "")
            break
        for call in calls:
            state["requested_tools"] += 1
            index = state["requested_tools"]
            function = call.get("function", {})
            name, args = function.get("name"), None
            tool_record = {"index": index, "request": number, "name": name,
                           "tool_call_id": call.get("id"), "started_utc": utc()}
            try:
                if index > MAX_TOOLS or time.monotonic() >= deadline:
                    raise ValueError("Learner tool or time budget exhausted")
                if name not in tools:
                    raise ValueError("Tool is outside the file-only capability allowlist")
                args = json.loads(function.get("arguments") or "{}")
                Draft202012Validator(schemas[name]).validate(args)
                if not isinstance(args, dict) or set(args) - set(schemas[name].get("properties", {})):
                    raise ValueError("Unknown tool argument")
                if ((name == "write_file" and args.get("area") == "scratch" and args.get("path") == "feedback.json") or
                        (name in ("write_file", "delete_path", "move_path") and args.get("area") == "adapter")):
                    coverage = evidence_coverage(required, state["evidence_reads_delivered"])
                    missing = [path for path, item in coverage.items() if not item["complete"]]
                    if missing:
                        raise ValueError("Read all task/prediction/trajectory chunks and receive them in a subsequent model request before feedback/knowledge writes; incomplete: " + ", ".join(missing))
                check_arguments(session, name, args)
                state["executed_tools"] += 1
                reply = await asyncio.wait_for(connection.call_tool(name, args),
                    timeout=min(90, max(.01, deadline - time.monotonic())))
                if any(item.type != "text" for item in reply.content):
                    raise ValueError("Unexpected non-text content from file-only broker")
                text = "\n".join(item.text for item in reply.content)
                ok = not (getattr(reply, "is_error", False) or getattr(reply, "isError", False))
                if ok:
                    state["successful_tools"] += 1
                    if args.get("area") in ("adapter", "scratch"):
                        expected = state["expected_candidate_hashes" if args["area"] == "adapter"
                                         else "expected_scratch_hashes"]
                        _update_expected(expected, name, args)
                    if name == "read_file" and args.get("area") == "evidence":
                        actual = json.loads(text)
                        path = safe_file(session / "evidence", args["path"])
                        original = path.read_text()
                        offset = args.get("offset", 0)
                        chunk = actual.get("text")
                        if not isinstance(chunk, str) or chunk != original[offset:offset + 30000]:
                            raise ValueError("Native read result does not match original evidence span")
                        read = {"path": args["path"], "start": offset, "end": offset + len(chunk),
                                "file_sha256": digest(path.read_bytes()), "tool_index": index, "read_request": number}
                        state["evidence_reads"].append(read)
                        pending_reads.append(read)
                tool_record.update(ok=ok, response=text)
            except Exception as exc:
                text = json.dumps({"ok": False, "error": (type(exc).__name__ + ": " + str(exc)).replace(key, "[REDACTED]")})
                tool_record.update(ok=False, response=text)
            tool_record["arguments"] = args
            save(session / f"control/fireworks-tool-results/{index:03}.json", tool_record)
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": text})
            checkpoint()
        checkpoint()
    else:
        state["status"] = "budget_exhausted"
    checkpoint()
    return state


async def _run_bridge(key, session, prompt, deadline, initial_hashes, transport):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    bootstrap = session / "control/file-broker-bootstrap.py"
    bootstrap.write_text(BROKER_BOOTSTRAP)
    save(session / "control/file-broker-transport.json", {
        "bootstrap": str(bootstrap), "sha256": digest(BROKER_BOOTSTRAP.encode()),
        "native_file_character_limit": 30000, "serialized_stdout_byte_limit": 512 * 1024,
        "change": "Bounded serialization capacity only; original OS sandbox execution and capabilities unchanged."})
    params = StdioServerParameters(command=sys.executable,
        args=[str(bootstrap), str(session)],
        env=broker_environment(session), cwd=str(session / "host"))
    with (session / "control/fireworks-mcp-stderr.log").open("w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=90) as connection:
                await connection.initialize()
                return await _conversation(key, connection, session, prompt, deadline, initial_hashes, transport)


def mechanical_admission(session, source, evidence, source_before, evidence_before,
                         prepared_before, state, definition):
    from harnessadapter.evolution.workspace import Workspace, fingerprint
    from harnessadapter.paths import PACKAGE_ROOT
    candidate = session / "adapter"
    base = json.loads((session / "control/baseline.json").read_text())
    admission = definition.finalize(candidate, session)
    validation = Workspace(session).validate()
    after = snapshot(candidate)
    hashes = {name: item["sha256"] for name, item in after.items()}
    copied_source = native_copied_files(source_before)
    changed = sorted(name for name in copied_source.keys() | after.keys()
                     if copied_source.get(name) != after.get(name))
    learned_changes = sorted(name for name in prepared_before.keys() | after.keys()
                             if prepared_before.get(name) != after.get(name))
    protected = {"source_strict": snapshot(source) == source_before,
                 "external_evidence_strict": snapshot(evidence) == evidence_before,
                 "source_native": fingerprint(source) == base["source"],
                 "framework": fingerprint(PACKAGE_ROOT, exclude=("evolvers", "architectures")) == base["framework"]}
    for area in ("evidence", "method", "environment", "history"):
        protected[area] = fingerprint(session / area) == base[area]
    forbidden = [name for name in changed if not allowed_candidate_path(name)]
    executable_markdown = [name for name in changed if name in after and after[name]["mode"] & 0o111]
    brief_path = candidate / "consumption/brief.md"
    brief = brief_path.read_text() if brief_path.is_file() else None
    provenance_ok = hashes == state.get("expected_candidate_hashes")
    coverage = evidence_coverage(required_evidence_files(session / "evidence"), state.get("evidence_reads_delivered", []))
    failures = []
    feedback = None
    try:
        feedback = feedback_document(session)
        if state.get("expected_scratch_hashes", {}).get("feedback.json") != feedback["sha256"]:
            failures.append("Public feedback does not match successful GLM-requested file operations")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        failures.append("Missing or invalid five-episode public GLM feedback: " + str(exc))
    if state.get("status") != "completed": failures.append("Learner did not finish normally")
    if not state.get("successful_tools"): failures.append("No successful native broker calls")
    if not state.get("evidence_reads"): failures.append("No evidence file read")
    if not all(entry["complete"] for entry in coverage.values()):
        failures.append("Complete task/prediction/trajectory text from every episode was not delivered to GLM")
    if not learned_changes: failures.append("No GLM knowledge change after native preparation")
    if forbidden: failures.append("Changes outside approved Markdown knowledge paths")
    if executable_markdown: failures.append("Executable permission on changed Markdown")
    if brief is None or len(brief) > MAX_BRIEF: failures.append("Missing or oversized consumption brief")
    if not all(protected.values()): failures.append("Protected input changed")
    if not validation["passed"]: failures.append("Native static validation failed")
    if not provenance_ok: failures.append("Candidate does not match successful GLM-requested operations")
    if state.get("response_models") != [MODEL]: failures.append("Unexpected model provenance")
    report = {"created_utc": utc(), "admitted": not failures, "failures": failures,
              "admission_mode": "automatic_mechanical_only", "human_semantic_gate": False,
              "model": MODEL, "source": str(source), "candidate": str(candidate),
              "changed_paths": changed, "glm_changed_paths": learned_changes,
              "forbidden_paths": forbidden,
              "native_copy_omissions": sorted(set(source_before) - set(copied_source)),
              "native_maintenance_paths": sorted(name for name in copied_source.keys() | prepared_before.keys()
                                                if copied_source.get(name) != prepared_before.get(name)),
              "protected_unchanged": protected, "operation_provenance_matches": provenance_ok,
              "public_feedback": feedback,
              "evidence_delivery_coverage": coverage,
              "validation": validation, "native_finalize": admission, "snapshot": after,
              "revision": revision(after), "brief_characters": len(brief) if brief is not None else None,
              "limits": ["Admission checks structure and provenance, not answer correctness or score improvement.",
                         "Knowledge feedback is the learner's interpretation without gold labels.",
                         "Native MWM maintenance is mechanical and separately recorded.",
                         "Public records omit private provider reasoning; no independent network attestation."]}
    diff = []
    for name in changed:
        if not name.endswith(".md"):
            continue
        old = (session / "history" / name).read_text().splitlines(keepends=True) if (session / "history" / name).is_file() else []
        new = (candidate / name).read_text().splitlines(keepends=True) if (candidate / name).is_file() else []
        diff.extend(difflib.unified_diff(old, new, fromfile="previous/" + name, tofile="candidate/" + name))
    (session / "control/knowledge-changes.diff").write_text("".join(diff))
    save(session / "control/mechanical-admission.json", report)
    return report


def run_learning(key, source: Path, evidence: Path, output: Path, block: int):
    """Run one new learning attempt, return active candidate (previous W on failure).

    ``output`` must not exist. For W0 use GENERIC_SOURCE; later ``source`` must be
    a prior admitted result's candidate. ``evidence`` must implement the manifest
    contract checked by validate_evidence. Caller owns API key and concurrency.
    Calls execute once, with no retry or human review. No paid work in tests.
    """
    source, evidence, output = (Path(p).absolute() for p in (source, evidence, output))
    if not isinstance(block, int) or isinstance(block, bool) or block < 1:
        raise ValueError("block must be a positive integer")
    if any(p.resolve() != p for p in (source, evidence, output)):
        raise ValueError("Use canonical directories without symlinks")
    for left, right in ((source, evidence), (source, output), (evidence, output)):
        if left == right or left in right.parents or right in left.parents:
            raise ValueError("Source, evidence and output must be separate non-nested directories")
    if output.exists():
        raise ValueError("Learning output already exists; attempts are never overwritten")
    output.mkdir(parents=True)
    start, stamp = time.monotonic(), utc()
    result = {"status": "rejected", "activated": False, "block": block, "model": MODEL,
              "source": str(source), "candidate": str(source), "started_utc": stamp,
              "transport": "fireworks", "admission_mode": "automatic_mechanical_only"}
    try:
        source_before, lineage = validate_source(source, block)
        evidence_before, manifest = validate_evidence(evidence, block)
        save(output / "source-before.json", source_before)
        save(output / "evidence-before.json", evidence_before)
        request = {"started_utc": stamp, "block": block, "model": MODEL, "goal": GOAL,
                   "source": str(source), "source_lineage": lineage,
                   "evidence": str(evidence), "evidence_revision": revision(evidence_before),
                   "episode_ids": [e["id"] for e in manifest["episodes"]],
                   "budgets": {"seconds": MAX_SECONDS, "requests": MAX_REQUESTS, "tools": MAX_TOOLS,
                               "completion_tokens": MAX_COMPLETION, "per_response": MAX_RESPONSE,
                               "reasoning_effort": 4096},
                   "network": "Agent file capabilities are network-disabled; controller uses only fixed Fireworks transport.",
                   "feedback": "Only GLM interpretation; no answer labels, manual/Astra feedback or semantic gate."}
        save(output / "request.json", request)
        # Import the new campaign transport, not any older experimental client.
        transport = importlib.import_module("client")
        if Path(transport.__file__).resolve().parent != ROOT or transport.MODEL != MODEL:
            raise ValueError("Learner requires this campaign's exact-model Fireworks transport")
        if str(REPOSITORY) not in sys.path:
            sys.path.insert(0, str(REPOSITORY))
        from harnessadapter.evolution.definition import EvolverDefinition
        from harnessadapter.evolution.session import evolve
        from harnessadapter.policy.execution import ExecutionPolicy
        definition = EvolverDefinition("mwm")
        prepared = evolve(source, evidence, GOAL, timeout=MAX_SECONDS, prepare_only=True,
            block=block, tick=block * 5, policy=ExecutionPolicy(backend="codex", network=False),
            definition=definition, source_kind="architecture" if source == GENERIC_SOURCE.resolve() else "adapter",
            output=output / "sessions", model=MODEL)
        session = Path(prepared["session"])
        prepared_before = snapshot(session / "adapter")
        save(output / "prepared-candidate.json", prepared_before)
        prompt = (session / "control/prompt.md").read_text()
        (output / "exact-prompt.md").write_text(prompt)
        result.update(session=str(session), proposed_candidate=prepared["candidate"],
                      native_prepare_result=str(session / "control/result.json"),
                      prompt_sha256=digest(prompt.encode()), evidence_revision=revision(evidence_before))
        remaining = MAX_SECONDS - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError("Native preparation consumed the learner deadline")
        hashes = {name: info["sha256"] for name, info in prepared_before.items()}
        state = asyncio.run(asyncio.wait_for(_run_bridge(key, session, prompt, start + MAX_SECONDS,
                                                       hashes, transport), timeout=remaining))
        result.update(learner_status=state["status"], model_calls=state["model_calls"],
                      requested_tools=state["requested_tools"], successful_tools=state["successful_tools"],
                      usage=state["usage"], response_models=state["response_models"])
        result.update({k:state[k] for k in ("usage_complete", "usage_unknown_calls", "wire_attempts",
                                          "rate_refusals", "scheduler_wait_seconds")})
        admission = mechanical_admission(session, source, evidence, source_before, evidence_before,
                                         prepared_before, state, definition)
        result.update(admission=str(session / "control/mechanical-admission.json"),
                      failures=admission["failures"], revision=admission["revision"],
                      public_feedback=admission["public_feedback"])
        if admission["admitted"]:
            result.update(status="admitted", activated=True, candidate=str(session / "adapter"))
    except Exception as exc:
        message = type(exc).__name__ + ": " + str(exc)
        result["error"] = message.replace(key, "[REDACTED]") if key else message
        result.setdefault("learner_status", "failed")
        if "session" in result:
            checkpoint = Path(result["session"]) / "control/fireworks-result.json"
            if checkpoint.is_file():
                partial = json.loads(checkpoint.read_text())
                result.update(model_calls=partial.get("model_calls", 0),
                              requested_tools=partial.get("requested_tools", 0), usage=partial.get("usage", {}))
                pending = partial.get("in_flight_request")
                result.update(usage_complete=partial.get("usage_complete", True) and not bool(pending),
                              usage_unknown_calls=partial.get("usage_unknown_calls", []) + ([pending] if pending else []),
                              wire_attempts=partial.get("wire_attempts", 0),
                              rate_refusals=partial.get("rate_refusals", 0),
                              scheduler_wait_seconds=partial.get("scheduler_wait_seconds", 0))
    result.update(finished_utc=utc(), elapsed_seconds=round(time.monotonic() - start, 3))
    save(output / "result.json", result)
    return result
