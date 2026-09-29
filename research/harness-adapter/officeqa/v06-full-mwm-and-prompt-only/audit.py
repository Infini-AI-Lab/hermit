"""Read-only saved-artifact audit. No runner imports/model calls; never opens private master keys.
Released completed-batch reference files are processed locally for hashes/schema only;
no reference contents are included in audit output."""
from __future__ import annotations
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
MODEL = "accounts/fireworks/models/glm-5p3-flash"
UPSTREAM = "https://api.fireworks.ai/inference/v1/responses"
from paths import CODEX as CODEX_PATH, REPOSITORY, SOURCE, DATA_CACHE, TASK
from experiment import ARCHITECTURE
CODEX = str(CODEX_PATH)
ARMS = ("baseline", "adapted")
COUNT, LEARN_EVERY = 50, 5
BLOCKS = COUNT // LEARN_EVERY
TOTAL_PREDICTIONS = COUNT * len(ARMS)
SEED = "metaworld-officeqa-pro-codex70-online-v1"


def file(path):
    path = Path(path)
    resolved = path.resolve()
    forbidden = {"answers.jsonl", "provenance.jsonl", "officeqa_pro.csv", "officeqa_full.csv"}
    # macOS /private/var is a system location, not the evaluator's private area.
    components = resolved.parts[2:] if resolved.parts[:2] == ("/", "private") else resolved.parts
    if path.is_symlink() or "private" in components or resolved.name in forbidden:
        raise ValueError("Auditor refuses private master-key contents and symlink files")
    if not resolved.is_file() or not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("Audit input must be a regular file")
    return resolved


def sha(path):
    with file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def text(path):
    return file(path).read_text(encoding="utf-8")


def read(path):
    return json.loads(text(path))


def numeric_records(paths):
    """Record counters outgrow their minimum filename padding (99 -> 100)."""
    return sorted(paths, key=lambda path: int(path.stem))


def declared_response_limit(source):
    """Resolve a frozen literal/default without importing or executing transports."""
    tree = ast.parse(source)
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "normalized":
            continue
        args = node.args.posonlyargs + node.args.args
        defaults = dict(zip([arg.arg for arg in args[-len(node.args.defaults):]], node.args.defaults)) if node.args.defaults else {}
        defaults.update({arg.arg: value for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None})
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                value = keyword.value
                if keyword.arg != "max_output_tokens" or not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "min" or not value.args:
                    continue
                bound = value.args[0]
                if isinstance(bound, ast.Name):
                    bound = defaults.get(bound.id)
                if isinstance(bound, ast.Constant) and isinstance(bound.value, int) and not isinstance(bound.value, bool):
                    values.append(bound.value)
    return values[0] if len(values) == 1 and values[0] > 0 else None


def lines(path):
    return [json.loads(line) for line in text(path).splitlines() if line.strip()]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def snapshot(root, detailed=False):
    root = Path(root)
    if root.resolve() != root.absolute() or not root.is_dir():
        raise ValueError("Snapshot root must be a canonical directory")
    result = {}
    for path in sorted(root.rglob("*")):
        if any(part in {".git", "__pycache__"} for part in path.relative_to(root).parts):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("Snapshot contains a link or special file")
        if stat.S_ISREG(mode):
            result[path.relative_to(root).as_posix()] = (
                {"sha256": sha(path), "bytes": path.stat().st_size, "mode": stat.S_IMODE(mode)}
                if detailed else sha(path))
    return result


def public(value):
    if isinstance(value, list):
        return [public(v) for v in value if not isinstance(v, dict) or v.get("type") != "reasoning"]
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items()
                if k not in ("reasoning_content", "encrypted_content", "api_key", "Authorization")}
    return value


def expected_prompt(question):
    return ("Answer the following question using the local references. Read README.md "
            "for the available file and search interfaces. Your final response must contain "
            "only the direct answer, one line, at most 250 characters.\n\nQuestion:\n" + question)


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def parse_policy(source):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', source)
    stack, roots = [], []
    for token in tokens:
        if token == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unbalanced policy")
            stack.pop()
        else:
            if not stack:
                raise ValueError("Unexpected policy atom")
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    if stack:
        raise ValueError("Unbalanced policy")
    return roots


def path_filters(node):
    result = []
    if isinstance(node, list):
        if len(node) == 2 and node[0] in ("subpath", "literal"):
            result.append((node[0], node[1]))
        for child in node:
            result.extend(path_filters(child))
    return result


def readable(form, target):
    if not form or form[0] != "allow" or not any(
        isinstance(item, str) and item in ("file-read*", "file-read-data", "file*")
        for item in form[1:]
    ):
        return False
    filters = path_filters(form)
    if not filters:
        return True
    target = Path(target).resolve()
    return any(target == Path(path).resolve() or kind == "subpath" and
               target.is_relative_to(Path(path).resolve()) for kind, path in filters)


class Audit:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.project = self.root.parent
        self.w0 = REPOSITORY / "harnessadapter/architectures" / ARCHITECTURE
        self.checks, self.pending = [], []
        self.observations = Counter()
        self.response_limit = None

    def check(self, name, ok, detail=None):
        item = {"name": name, "passed": bool(ok)}
        if detail is not None:
            item["detail"] = detail
        self.checks.append(item)

    def guard(self, name, fn):
        try:
            return fn()
        except Exception as exc:
            self.check(name + ": readable/well-formed", False, {"exception_type": type(exc).__name__})

    def local(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.root) or "private" in path.relative_to(self.root).parts:
            raise ValueError("Audit reference outside campaign")
        return path

    def adapter(self, value):
        path = Path(value).resolve()
        if path == self.w0:
            return path
        if not path.is_relative_to(self.root / "evolution") or path.name != "adapter":
            raise ValueError("Not a candidate from this campaign")
        return path

    def policy(self, prefix, invocation, workspace, records):
        argv, settings = invocation["argv"], invocation["settings"]
        self.check(prefix + ": real Codex under OS sandbox", argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and argv[3] == CODEX)
        forms = parse_policy(argv[2])
        provider = settings["model_providers.officeqa_fireworks"]
        endpoint = urlparse(provider["base_url"])
        self.check(prefix + ": default deny", ["deny", "default"] in forms)
        self.check(prefix + ": local per-task endpoint", endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and bool(endpoint.port) and endpoint.path == "/v1" and not endpoint.query and not endpoint.username)
        net = [f for f in forms if f and f[0] == "allow" and any(isinstance(v, str) and v.startswith("network") for v in f[1:])]
        self.check(prefix + ": only own broker TCP", net == [["allow", "network-outbound", ["remote", "tcp", f"localhost:{endpoint.port}"]]])
        forbidden = [self.root / "prepared/private/answers.jsonl", self.root / "prepared/private/provenance.jsonl",
                     self.root / "prepared/agent/tasks.jsonl", self.root / "prepared/manifest.json",
                     self.root / "state.json", self.root / "private-evaluation/paired-metrics.json",
                     records / "events.jsonl", self.root / "evidence", self.root / "private-learning-seals",
                     DATA_CACHE / "officeqa_pro.csv", DATA_CACHE / "download_receipt.json",
                     TASK / "results", self.root / ".task-paths.json"]
        forbidden += [self.root / "runs" / arm / f"{n:03}/workspace/final.md"
                      for arm in ARMS for n in range(1, COUNT + 1)
                      if self.root / "runs" / arm / f"{n:03}/workspace" != workspace]
        forbidden += [other / "state.json" for other in self.project.iterdir()
                      if other.is_dir() and other.resolve() != self.root]
        exposed = [str(p) for p in forbidden if any(readable(f, p) for f in forms)]
        self.check(prefix + ": no direct gold/future/other-arm/controller read grants", not exposed, {"unexpected_grants": exposed})
        permitted = [workspace, self.root / "prepared/agent/corpus", self.root / "prepared/agent/corpus.sqlite"]
        if invocation.get("adapter"):
            permitted.append(self.adapter(invocation["adapter"]))
        unexpected = []
        for form in forms:
            if not form or form[0] != "allow" or not any(isinstance(v, str) and v in ("file-read*", "file-read-data", "file*") for v in form[1:]):
                continue
            for _, value in path_filters(form):
                path = Path(value).resolve()
                if path.is_relative_to(self.root) and not any(path == allowed or path.is_relative_to(allowed) for allowed in permitted):
                    unexpected.append(str(path.relative_to(self.root)))
        self.check(prefix + ": no additional campaign memory/read roots", not unexpected, {"unexpected_roots": unexpected})
        for target in (workspace, self.root / "prepared/agent/corpus", self.root / "prepared/agent/corpus.sqlite"):
            self.check(prefix + ": required readable " + target.name, any(readable(f, target) for f in forms))
        writes = [p for f in forms if f and f[0] == "allow" and "file-write*" in f for _, p in path_filters(f) if not p.startswith("/dev/")]
        self.check(prefix + ": only own folder writable", writes == [str(workspace)])
        protected = {str(workspace / n) for n in ("AGENTS.md", "README.md", "officeqa.py", ".codex", ".acting-prepared.json", ".ha-demo-generated.json")}
        denied = {p for f in forms if f and f[0] == "deny" and "file-write*" in f for _, p in path_filters(f)}
        self.check(prefix + ": fixed instructions protected", protected.issubset(denied))
        self.check(prefix + ": exact GLM Responses provider", settings.get("model") == MODEL and settings.get("model_provider") == "officeqa_fireworks" and provider.get("wire_api") == "responses" and provider.get("requires_openai_auth") is False and "env_key" not in provider)
        self.check(prefix + ": internal sandbox disabled only inside external one", settings.get("sandbox_mode") == "danger-full-access" and argv[:2] == ["/usr/bin/sandbox-exec", "-p"])
        self.check(prefix + ": no global context/web/approvals", settings.get("approval_policy") == "never" and settings.get("web_search") == "disabled" and all(v in argv for v in ("--ignore-user-config", "--ignore-rules", "--ephemeral")))
        self.check(prefix + ": delegates and external memories disabled", settings.get("agents.enabled") is False and all(settings.get("features." + v) is False for v in ("multi_agent", "multi_agent_v2", "memories", "apps", "plugins", "browser_use")))

    def tool(self, prefix, value):
        kind = value.get("type")
        self.check(prefix + ": local tool schema", kind in ("function", "custom", "namespace") and not any(k in value for k in ("server_url", "server_label", "connector_id")))
        if kind == "namespace":
            for child in value.get("tools", []):
                self.tool(prefix, child)

    def actor(self, arm, row, task, adapter):
        ordinal = row["ordinal"]
        prefix = f"{arm}/{ordinal:03}"
        base = self.root / "runs" / arm / f"{ordinal:03}"
        workspace, records = base / "workspace", base / "records"
        self.check(prefix + ": exact frozen question", read(base / "task.json") == {"ordinal": ordinal, **task})
        self.check(prefix + ": original summary equals state", read(base / "summary.json") == row)
        self.check(prefix + ": prediction hash and text", sha(base / "prediction.json") == row["prediction_sha256"] and read(base / "prediction.json") == {"task_id": task["task_id"], "answer": row["answer"]})
        invocation, result = read(records / "invocation.json"), read(records / "result.json")
        self.check(prefix + ": exact question prompt", file(records / "prompt.md").read_bytes() == expected_prompt(task["question"]).encode())
        self.check(prefix + ": unchanged prompt/instructions", sha(records / "prompt.md") == invocation["prompt_sha256"] and sha(workspace / "AGENTS.md") == invocation["agents_sha256"])
        self.check(prefix + ": requested actual model", invocation.get("requested_model") == result.get("requested_model") == MODEL)
        self.check(prefix + ": identical full corpus/index", invocation.get("corpus") == str(self.root / "prepared/agent/corpus") and invocation.get("index") == str(self.root / "prepared/agent/corpus.sqlite"))
        self.policy(prefix, invocation, workspace, records)
        marker = read(workspace / ".acting-prepared.json")
        self.check(prefix + ": correct batch adapter", invocation.get("adapter") == marker.get("adapter") == (str(adapter) if adapter else None))
        if adapter:
            self.check(prefix + ": batch adapter hash unchanged", row.get("adapter_revision") == digest(snapshot(adapter)))
        else:
            self.check(prefix + ": baseline no adapter/MCP", row.get("adapter_revision") is None and not any(k.startswith("mcp_servers.") for k in invocation["settings"]))
        for name, expected in result.get("record_hashes", {}).items():
            self.check(prefix + ": immutable record " + name, Path(name).name == name and sha(records / name) == expected)
        events = lines(records / "events.jsonl")
        messages = [e["item"]["text"] for e in events if e.get("type") == "item.completed" and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message" and isinstance(e["item"].get("text"), str)]
        final, validation = records / "final.md", result.get("final_validation", {})
        if validation.get("valid"):
            self.check(prefix + ": final file matches native event/hash", final.is_file() and not final.is_symlink() and final.stat().st_size <= 1048576 and sha(final) == validation.get("sha256") and bool(messages) and text(final).strip() == messages[-1].strip())
        else:
            self.check(prefix + ": rejected final not copied", not final.exists())
        self.check(prefix + ": original answer eligibility", (validation.get("valid") and row["answer"] == text(final).strip()) if row.get("status") == "completed" else row["answer"] == "")
        self.check(prefix + ": no grader fields in actor", not any(k in result or k in row for k in ("score", "grade", "ground_truth", "gold_answer")))
        transport = read(base / "api/summary.json")
        dirs = numeric_records(p for p in (base / "api").iterdir() if p.is_dir() and p.name.isdigit())
        self.check(prefix + ": declared request count", len(dirs) == transport.get("calls") == row.get("requests"))
        limits = transport.get("limits", {})
        self.check(prefix + ": upstream and budget fixed", transport.get("model") == MODEL and transport.get("upstream") == UPSTREAM and "requests" in limits and limits["requests"] is None and limits.get("completion_tokens") == 65536)
        if "per_response" in limits:
            self.check(prefix + ": recorded per-response cap matches frozen default", limits["per_response"] == self.response_limit)
        output_tokens, unknown = 0, []
        for directory in dirs:
            label = prefix + "/api/" + directory.name
            request = read(directory / "request.json")
            self.check(label + ": exact stateless model request", request.get("model") == MODEL and request.get("store") is False and not request.get("previous_response_id"))
            self.check(label + ": frozen remaining-output limit", isinstance(self.response_limit, int) and isinstance(request.get("max_output_tokens"), int) and 0 < request["max_output_tokens"] <= min(self.response_limit, 65536-output_tokens))
            for tool in request.get("tools", []):
                self.tool(label, tool)
            if (directory / "response.json").is_file():
                response = read(directory / "response.json")
                self.check(label + ": actual returned model", response.get("model") == MODEL)
                self.observations["actor_responses"] += 1
            if (directory / "transport.json").is_file():
                record = read(directory / "transport.json")
                self.check(label + ": exact upstream metadata", record.get("upstream") == UPSTREAM and record.get("model") == MODEL)
                if record.get("response_model"):
                    self.check(label + ": response model metadata", record["response_model"] == MODEL)
                output_tokens += int((record.get("usage") or {}).get("output_tokens") or 0)
                attempts = record.get("attempts", [])
                if attempts:
                    request_hash = record.get("request_sha256", "")
                    self.check(label + ": every wire attempt uses identical recorded request hash", bool(re.fullmatch(r"[0-9a-f]{64}", request_hash)) and all(a.get("request_sha256") == request_hash for a in attempts))
                    self.check(label + ": every returned wire model exact", all(a.get("response_model") in (None, MODEL) for a in attempts))
                    self.check(label + ": all wire usage included", all(int((record.get("usage") or {}).get(k) or 0) == sum(int((a.get("usage") or {}).get(k) or 0) for a in attempts) for k in ("input_tokens", "output_tokens", "total_tokens")))
                if record.get("usage_complete") is False or not record.get("usage"):
                    unknown.append(int(directory.name))
            else:
                unknown.append(int(directory.name))
        declared = set(transport.get("usage_unknown_calls", [])) | set(row.get("usage_unknown_calls", []))
        self.check(prefix + ": unknown costs retained explicitly", set(unknown).issubset(declared) and (not unknown or row.get("usage_complete") is False or transport.get("usage_complete") is False))
        self.check(prefix + ": aggregate output budget", output_tokens <= 65536)
        self.check(prefix + ": total known output includes failed calls", output_tokens == int((transport.get("usage") or {}).get("output_tokens") or 0))
        self.observations["actor_calls_unknown_usage"] += len(unknown)
        self.observations["completed_actors_audited"] += 1
        return {"common": marker["common_instructions_sha256"], "prompt": invocation["prompt_sha256"], "finished": result.get("finished_utc")}

    def reference_release(self, block, manifest, evidence, selected, learner_started):
        """Check locally released batch files against a controller receipt, never print answers."""
        prefix = f"learning/{block:02}/reference-release"
        descriptor = manifest.get("batch_seal") or {}
        expected_name = f"{block:02}.json"
        self.check(prefix + ": fixed controller receipt path", descriptor.get("file") == expected_name)
        seal_path = self.root / "private-learning-seals" / expected_name
        seal = read(seal_path)
        self.check(prefix + ": receipt digest", sha(seal_path) == descriptor.get("sha256"))
        self.check(prefix + ": completed-batch-only declaration", seal.get("block") == block and
                   seal.get("source_arm") == "adapted" and seal.get("predictions_sealed") is True and
                   seal.get("reference_scope") == "completed_batch_only")
        ids = [item["task_id"] for item in selected]
        ordinals = list(range((block-1)*LEARN_EVERY+1, block*LEARN_EVERY+1))
        entries = seal.get("episodes", [])
        self.check(prefix + ": exact current five IDs/ordinals",
                   len(entries) == LEARN_EVERY and [item.get("id") for item in entries] == ids and
                   [item.get("ordinal") for item in entries] == ordinals)
        declared_episodes = manifest.get("episodes", [])
        self.check(prefix + ": manifest exact directory/ordinal sequence",
                   [item.get("ordinal") for item in declared_episodes] == ordinals and
                   [item.get("directory") for item in declared_episodes] == [f"{n:03}" for n in ordinals])
        by_id = {item.get("id"): item for item in entries}
        sealed_time = timestamp(seal["sealed_utc"]) if seal.get("sealed_utc") else None
        release_time = timestamp(seal["reference_released_utc"]) if seal.get("reference_released_utc") else None
        if sealed_time is not None or release_time is not None:
            self.check(prefix + ": release follows seal and precedes learning",
                       sealed_time is not None and release_time is not None and
                       sealed_time <= release_time <= timestamp(learner_started))
        for task, ordinal in zip(selected, ordinals):
            item = by_id.get(task["task_id"], {})
            reference = evidence / f"{ordinal:03}" / "reference_answer.json"
            body = read(reference)
            original = self.root / "runs/adapted" / f"{ordinal:03}"
            terminal = read(original / "records/result.json")
            self.check(prefix + f": Q{ordinal} reference schema/current ID",
                       set(body) == {"task_id", "official_answer"} and body.get("task_id") == task["task_id"] and
                       isinstance(body.get("official_answer"), str) and bool(body["official_answer"].strip()))
            self.check(prefix + f": Q{ordinal} task/prediction/reference hash binding",
                       item.get("task_sha256") == sha(original / "task.json") and
                       item.get("prediction_sha256") == sha(original / "prediction.json") and
                       item.get("reference_answer_sha256") == sha(reference))
            finished = terminal.get("finished_utc")
            self.check(prefix + f": Q{ordinal} ended before learning and seal",
                       bool(finished) and timestamp(finished) <= timestamp(learner_started) and
                       (sealed_time is None or timestamp(finished) <= sealed_time))
        self.observations["sealed_reference_batches"] += 1

    def learning(self, block, row, source, tasks):
        prefix = f"learning/{block:02}"
        output = self.root / "evolution" / f"{block:02}"
        result = read(output / "result.json")
        self.check(prefix + ": original result/state match", all(row.get(k) == v for k, v in result.items()))
        self.check(prefix + ": accepted-source lineage", result.get("source") == str(source))
        self.check(prefix + ": exact GLM-only mechanical method", result.get("model") == MODEL and result.get("transport") == "fireworks" and result.get("admission_mode") == "automatic_mechanical_only")
        selected = tasks[(block-1)*5:block*5]
        ids = [t["task_id"] for t in selected]
        evidence = self.root / "evidence" / f"{block:02}"
        manifest = read(evidence / "manifest.json")
        self.check(prefix + ": only own previous five episodes", manifest.get("source_arm") == "adapted" and manifest.get("block") == block and manifest.get("includes_gold") is True and [e.get("id") for e in manifest.get("episodes", [])] == ids and all(manifest.get(k) is False for k in ("includes_grades", "includes_future_questions", "includes_baseline_traces", "includes_source_hints")))
        self.reference_release(block, manifest, evidence, selected, result["started_utc"])
        controller_receipt = result.get("controller_batch_seal") or {}
        self.check(prefix + ": learner result binds controller batch seal",
                   controller_receipt.get("path") == str(self.root / "private-learning-seals" / f"{block:02}.json") and
                   controller_receipt.get("sha256") == manifest["batch_seal"].get("sha256") and
                   controller_receipt.get("block") == block and controller_receipt.get("episode_ids") == ids)
        allowed = {"README.md", "manifest.json"}
        for entry in manifest.get("files", []):
            relative = entry["path"]
            allowed.add(relative)
            copied = (evidence / relative).resolve()
            if not copied.is_relative_to(evidence):
                raise ValueError("Evidence path escaped")
            ordinal = int(Path(relative).parts[0])
            self.check(prefix + ": evidence hash/ordinal " + relative, sha(copied) == entry["sha256"] and (block-1)*5 < ordinal <= block*5)
            self.check(prefix + ": authorized evidence artifact " + relative, len(Path(relative).parts) == 2 and
                       Path(relative).name in ("task.json", "prediction.json", "summary.json", "trajectory.jsonl", "reference_answer.json", "failure.json"))
            original = self.root / "runs/adapted" / f"{ordinal:03}"
            if Path(relative).name in ("task.json", "prediction.json", "summary.json"):
                self.check(prefix + ": copied original " + relative, sha(copied) == sha(original / Path(relative).name))
            elif Path(relative).name == "trajectory.jsonl":
                # Public completed items plus the terminal turn events (v6 keeps the failure reason visible).
                selected_events = [public(e) for e in lines(original / "records/events.jsonl") if e.get("type") in ("turn.completed", "turn.failed") or (
                    e.get("type") == "item.completed" and isinstance(e.get("item"), dict) and e["item"].get("type") in ("agent_message", "command_execution", "mcp_tool_call", "file_change"))]
                self.check(prefix + ": exact public extraction " + relative, lines(copied) == selected_events)
        self.check(prefix + ": no additional evidence files", {p.relative_to(evidence).as_posix() for p in evidence.rglob("*") if p.is_file()} == allowed)
        if (output / "request.json").exists():
            request = read(output / "request.json")
            self.check(prefix + ": explicit model/source/IDs", request.get("model") == MODEL and request.get("source") == str(source) and request.get("episode_ids") == ids)
            self.check(prefix + ": request/result controller seal agree", request.get("controller_batch_seal") == controller_receipt)
            budgets = request.get("budgets") or {}
            self.check(prefix + ": no request/tool-count cap and fixed time/output budgets",
                       budgets.get("seconds") == 1800 and budgets.get("request_and_tool_counts") == "uncapped" and
                       budgets.get("completion_tokens") == 65536 and budgets.get("per_response") == 65536)
            self.check(prefix + ": source/evidence unchanged", snapshot(source, True) == read(output / "source-before.json") and snapshot(evidence, True) == read(output / "evidence-before.json"))
        admitted = result.get("activated") is True
        if not admitted:
            self.check(prefix + ": rejected update retains old W", result.get("candidate") == str(source))
        if not result.get("session"):
            self.check(prefix + ": no session means no activation", not admitted)
            return source
        session = self.local(result["session"])
        prompt = text(output / "exact-prompt.md")
        self.check(prefix + ": exact native learning prompt", prompt == text(session / "control/prompt.md") and hashlib.sha256(prompt.encode()).hexdigest() == result.get("prompt_sha256"))
        prepared = read(output / "prepared-candidate.json")
        expected = {name: value["sha256"] for name, value in prepared.items()}
        scratch, grouped = {}, {}
        for path in numeric_records((session / "control/fireworks-tool-results").glob("*.json")):
            tool = read(path)
            grouped.setdefault(tool["request"], []).append(tool)
        messages = [{"role": "user", "content": prompt}]
        calls = numeric_records((session / "control/fireworks-calls").glob("*.json"))
        known_usage = Counter()
        unknown_usage = []
        for path in calls:
            call = read(path)
            label = prefix + "/call/" + str(call["request"])
            self.check(label + ": GLM requested", call.get("requested_model") == MODEL)
            self.check(label + ": numeric reasoning and remaining-output bound",
                       call.get("reasoning_effort") == 4096 and isinstance(call.get("request_max_tokens"), int) and
                       0 < call["request_max_tokens"] <= 65536-known_usage["completion_tokens"])
            self.check(label + ": independently reconstructed public history", call.get("public_request_sha256") == digest(messages) and call.get("public_message_count") == len(messages))
            meta = call.get("meta", {})
            for name in ("prompt_tokens", "completion_tokens"):
                known_usage[name] += int((meta.get("usage") or {}).get(name) or 0)
            if meta.get("usage_complete") is False or not meta.get("usage"):
                unknown_usage.append(call["request"])
            attempts = meta.get("attempts", [])
            if attempts:
                self.check(label + ": every wire response model exact", all(a.get("response_model") in (None, MODEL) for a in attempts))
                self.check(label + ": all wire usage included", all(int((meta.get("usage") or {}).get(k) or 0) == sum(int((a.get("usage") or {}).get(k) or 0) for a in attempts) for k in ("prompt_tokens", "completion_tokens")))
            if meta.get("response_model"):
                self.check(label + ": exact returned model", meta["response_model"] == MODEL)
                self.observations["learner_responses"] += 1
            else:
                self.check(label + ": missing response cannot activate", not admitted)
                self.observations["learner_calls_without_response_model"] += 1
            message = call.get("message")
            if not isinstance(message, dict):
                continue
            messages.append(message)
            requested = {c.get("id"): c.get("function", {}) for c in message.get("tool_calls", [])}
            for tool in grouped.get(call["request"], []):
                function = requested.get(tool.get("tool_call_id"), {})
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = None
                self.check(label + ": tool operation originates in GLM", function.get("name") == tool.get("name") and args == tool.get("arguments"))
                messages.append({"role": "tool", "tool_call_id": tool.get("tool_call_id"), "content": tool["response"]})
                args = tool.get("arguments") or {}
                if tool.get("ok") and args.get("area") in ("adapter", "scratch"):
                    target = expected if args["area"] == "adapter" else scratch
                    if tool["name"] == "write_file":
                        target[args["path"]] = hashlib.sha256(args["content"].encode()).hexdigest()
                    elif tool["name"] == "delete_path":
                        target.pop(args["path"], None)
                    elif tool["name"] == "move_path":
                        target[args["destination"]] = target.pop(args["source"])
        self.check(prefix + ": saved call count", len(calls) == result.get("model_calls", len(calls)))
        self.check(prefix + ": all known usage retained including failed calls", all(int((result.get("usage") or {}).get(k) or 0) == known_usage[k] for k in ("prompt_tokens", "completion_tokens")))
        self.check(prefix + ": learner aggregate output budget", known_usage["completion_tokens"] <= 65536)
        if "usage_complete" in result:
            self.check(prefix + ": unknown learner costs explicit", set(unknown_usage).issubset(set(result.get("usage_unknown_calls", []))) and (not unknown_usage or result["usage_complete"] is False))
        if not admitted:
            self.observations["rejected_learning_updates"] += 1
            return source
        candidate = self.adapter(result["candidate"])
        receipt = read(self.local(result["admission"]))
        self.check(prefix + ": admission/controller batch seal agree", receipt.get("controller_batch_seal") == controller_receipt)
        current = snapshot(candidate, True)
        self.check(prefix + ": mechanical admission without human gate", result.get("status") == "admitted" and receipt.get("admitted") is True and receipt.get("human_semantic_gate") is False and receipt.get("admission_mode") == "automatic_mechanical_only")
        self.check(prefix + ": no admission failures", receipt.get("failures") == [] and receipt.get("forbidden_paths") == [] and receipt.get("validation", {}).get("passed") is True and all(receipt.get("protected_unchanged", {}).values()))
        self.check(prefix + ": final candidate equals reconstructed GLM writes", {n: v["sha256"] for n, v in current.items()} == expected and receipt.get("operation_provenance_matches") is True)
        self.check(prefix + ": candidate snapshot/revision", current == receipt.get("snapshot") and digest(current) == receipt.get("revision") == result.get("revision"))
        self.check(prefix + ": only GLM response-model set", result.get("response_models") == [MODEL])
        feedback = result.get("public_feedback") or {}
        fp = self.local(feedback["path"])
        self.check(prefix + ": feedback equals last successful GLM write", fp == session / "scratch/feedback.json" and sha(fp) == feedback.get("sha256") == scratch.get("feedback.json"))
        body = read(fp)
        self.check(prefix + ": five GLM reference-answer comparisons", body.get("label") == "glm_reference_answer_feedback" and {e.get("id") for e in body.get("episodes", [])} == set(ids))
        coverage = receipt.get("evidence_delivery_coverage", {})
        required = {f"{n:03}/{name}" for n in range((block-1)*5+1, block*5+1)
                    for name in ("task.json", "prediction.json", "trajectory.jsonl", "reference_answer.json")}
        self.check(prefix + ": all four evidence files per episode delivered completely",
                   set(coverage) == required and all(v.get("complete") is True for v in coverage.values()))
        self.observations["admitted_learning_updates"] += 1
        return candidate

    def grading(self, state, tasks, actors):
        evaluation = self.root / "private-evaluation"
        seal_path = evaluation / "predictions-seal.json"
        grades = [evaluation / f"{arm}-grades.json" for arm in ARMS]
        metrics_path = evaluation / "paired-metrics.json"
        any_grades = any(p.exists() for p in [*grades, metrics_path]) or state.get("metrics") is not None
        self.check("grading: no grades without paired seal", not any_grades or seal_path.is_file())
        if not seal_path.is_file():
            self.pending.append("Paired seal and scoring not yet present")
            return False
        if state.get("status") != "completed" and (len(actors) < TOTAL_PREDICTIONS or len(state.get("learning", [])) < BLOCKS):
            self.pending.append("Seal appeared after the initial incomplete state snapshot; audit again after completion")
            return False
        seal = read(seal_path)
        ids = [t["task_id"] for t in tasks]
        self.check("grading: 100 predictions sealed together", seal.get("total_predictions") == TOTAL_PREDICTIONS and set(seal.get("arms", {})) == set(ARMS))
        self.check("grading: frozen manifest link", seal.get("manifest_sha256") == sha(self.root / "prepared/manifest.json"))
        moment = timestamp(seal["sealed_utc"])
        for arm in ARMS:
            entry = seal["arms"][arm]
            path = self.local(entry["path"])
            rows = lines(path)
            originals = {r["task_id"]: r for r in state["arms"][arm]["results"]}
            self.check("grading: " + arm + " exact IDs/order/hash", sha(path) == entry["sha256"] and [r.get("task_id") for r in rows] == ids and len(rows) == COUNT)
            self.check("grading: " + arm + " original predictions unchanged", all(set(r) == {"task_id", "answer"} and r["answer"] == originals[r["task_id"]]["answer"] for r in rows))
        self.check("grading: all actors finished before seal", len(actors) == TOTAL_PREDICTIONS and all(a.get("finished") and timestamp(a["finished"]) <= moment for a in actors))
        self.check("grading: 10 completed learning events before seal", len(state.get("learning", [])) == BLOCKS and all(timestamp(r["finished_utc"]) <= moment for r in state["learning"]))
        for arm, path in zip(ARMS, grades):
            if not path.is_file():
                self.pending.append(arm + " grade file not yet present")
                continue
            grade = read(path)
            self.check("grading: " + arm + " timestamp after seal", path.stat().st_mtime >= moment)
            self.check("grading: " + arm + " metric and prediction linkage", grade.get("denominator") == COUNT and grade.get("primary_tolerance") == 0 and grade.get("predictions_sha256") == seal["arms"][arm]["sha256"] and len(grade.get("results", [])) == COUNT)
            self.check("grading: " + arm + " no gold fields exported", all(not any(k in row for k in ("answer", "ground_truth", "gold_answer", "source_docs", "source_files")) for row in grade["results"]))
        if metrics_path.is_file():
            metrics = read(metrics_path)
            self.check("grading: saved paired metrics match seal", metrics.get("seal_sha256") == sha(seal_path) and timestamp(metrics["graded_utc"]) >= moment)
            if state.get("metrics") is not None:
                self.check("grading: saved paired metrics match state", metrics == state["metrics"])
            else:
                self.pending.append("Metrics appeared after the initial state snapshot")
        else:
            self.pending.append("Paired metrics not yet present")
        return all(p.is_file() for p in [*grades, metrics_path])

    def execute(self):
        frozen = read(self.root / "freeze.json")
        state_bytes = file(self.root / "state.json").read_bytes()
        state = json.loads(state_bytes)
        manifest = read(self.root / "prepared/manifest.json")
        source_hashes = {"freeze": sha(self.root / "freeze.json"), "state": hashlib.sha256(state_bytes).hexdigest()}
        source_hashes.update(manifest=sha(self.root / "prepared/manifest.json"), auditor=sha(__file__))
        for name, expected in frozen["files"].items():
            self.check("freeze: " + name, Path(name).name == name and sha(self.root / name) == expected)
        if 'framework_files' in frozen:
            self.check('freeze: framework package', snapshot(REPOSITORY / 'harnessadapter') == frozen['framework_files'])
            self.check('freeze: task deployment', snapshot(self.root / 'deployment') == frozen['deployment_files'])
        # Read the frozen literal without importing/executing a model transport.
        self.response_limit = declared_response_limit(text(self.root / "broker.py"))
        self.check("freeze: unique declared per-response limit", isinstance(self.response_limit, int))
        self.check("freeze: corpus helper", sha(SOURCE / "officeqa_tools.py") == frozen["reference_tool_sha256"])
        self.check("freeze: manifest", sha(self.root / "prepared/manifest.json") == frozen["manifest_sha256"])
        self.check("freeze: generic W0", snapshot(self.w0) == frozen["generic_W0"] and digest(snapshot(self.w0)) == frozen["generic_W0_revision"])
        self.check("freeze: model/cadence", frozen.get("model") == state.get("model") == MODEL and frozen.get("learn_every") == state.get("learn_every") == 5)
        tasks = lines(self.root / "prepared/agent/tasks.jsonl")
        ids = [t["task_id"] for t in tasks]
        self.check("data: 50 sanitized unique tasks", len(tasks) == len(set(ids)) == COUNT and all(set(t) == {"task_id", "question"} for t in tasks))
        self.check("data: exact frozen task order/hash", ids == manifest["task_ids"] and sha(self.root / "prepared/agent/tasks.jsonl") == manifest["tasks_sha256"])
        self.check("data: declared deterministic independent seed", manifest.get("selection", {}).get("seed") == SEED and manifest.get("sample_count") == COUNT and manifest.get("source_question_count") == 133)
        learning_policy = manifest.get("learner_policy") or {}
        self.check("data: gold feedback scope explicitly declared", learning_policy.get("includes_gold") is True and
                   learning_policy.get("reference_scope") == "completed_batch_only" and
                   learning_policy.get("master_answer_key_mounted") is False and
                   learning_policy.get("source_hints_released") is False)
        self.check("data: complete shared corpus/index", len(list((self.root / "prepared/agent/corpus").glob("*.txt"))) == 697 and sha(self.root / "prepared/agent/corpus.sqlite") == manifest["index_sha256"])
        metadata = read(self.root / "prepared/index-metadata.json")
        self.check("data: index declares no answers/source hints", metadata.get("uses_answers") is False and metadata.get("uses_question_source_hints") is False)
        learning = {r["block"]: r for r in state.get("learning", [])}
        source, by_batch = self.w0, {}
        for block in range(1, BLOCKS + 1):
            by_batch[block] = source
            if block in learning:
                new = self.guard(f"learning/{block:02}", lambda b=block, s=source: self.learning(b, learning[b], s, tasks))
                if new is not None:
                    source = new
            else:
                self.pending.append(f"Learning block {block} unfinished")
        actors, pairs = [], {}
        for arm in ARMS:
            arm_state = state.get("arms", {}).get(arm, {})
            rows = arm_state.get("results", [])
            ordinals = [r.get("ordinal") for r in rows]
            self.check(arm + ": counter/unique attempts", arm_state.get("completed") == len(rows) == len(set(ordinals)) and all(isinstance(n, int) and 1 <= n <= COUNT for n in ordinals))
            self.check(arm + ": fixed task identities", all(r.get("task_id") == ids[r["ordinal"]-1] for r in rows))
            for row in sorted(rows, key=lambda r: r["ordinal"]):
                ordinal, batch = row["ordinal"], (row["ordinal"]-1)//5 + 1
                if arm == "adapted":
                    self.check(f"adapted/{ordinal:03}: prior learning barrier", all(b in learning for b in range(1, batch)))
                adapter = by_batch[batch] if arm == "adapted" else None
                detail = self.guard(f"{arm}/{ordinal:03}", lambda a=arm, r=row, w=adapter: self.actor(a, r, tasks[r["ordinal"]-1], w))
                if detail:
                    actors.append(detail)
                    pairs.setdefault(ordinal, {})[arm] = detail
            if len(rows) < COUNT:
                self.pending.append(f"{arm}: {COUNT-len(rows)} results not completed at snapshot")
        for ordinal, pair in pairs.items():
            if set(pair) == set(ARMS):
                self.check(f"pair/{ordinal:03}: identical task/common instructions", pair["baseline"]["prompt"] == pair["adapted"]["prompt"] and pair["baseline"]["common"] == pair["adapted"]["common"])
        grading = self.guard("grading", lambda: self.grading(state, tasks, actors))
        complete = state.get("status") == "completed" and len(actors) == TOTAL_PREDICTIONS and len(learning) == BLOCKS and grading is True
        if state.get("status") == "completed":
            self.check("complete campaign has all artifacts", complete)
        for name, expected in frozen["files"].items():
            self.check("freeze after audit: " + name, sha(self.root / name) == expected)
        failures = [c for c in self.checks if not c["passed"]]
        return {"created_utc": datetime.now(timezone.utc).isoformat(),
                "scope": "Read-only saved-artifact consistency audit; released batch references processed locally, never printed; no private master keys, model calls, score recomputation or experiment edits.",
                "campaign_status_at_snapshot": state.get("status"), "complete_campaign_audited": complete,
                "status": "failed" if failures else "complete_checks_passed" if complete else "partial_checks_passed",
                "passed": not failures, "counts": {"checks": len(self.checks), "passed": len(self.checks)-len(failures), "failed": len(failures), "pending": len(self.pending)},
                "source_hashes": source_hashes, "observations": dict(self.observations),
                "failures": failures, "pending": self.pending, "checks": self.checks,
                "limits": ["File consistency is not independent network attestation.",
                           "Provider private reasoning is omitted from public logs.",
                           "Recorded sandbox policy is inspected; historical enforcement relies on preflight evidence.",
                           "Private master answer keys are never opened; released batch references are checked for schema/hash bindings, not independently rederived. Scores are timing-linked, not recalculated.",
                           "File timestamps support ordering but are not cryptographically trusted.",
                           "Partial audits consider only completed episodes present in the initial state snapshot."]}


def run(root: Path = ROOT):
    audit = Audit(root)
    result = audit.execute()
    directory = audit.root / "preflight"
    directory.mkdir(exist_ok=True)
    output = directory / ("final-audit.json" if result["campaign_status_at_snapshot"] == "completed" else "partial-audit.json")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".audit-", suffix=".json", dir=directory, delete=False) as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, output)
    result["audit_output"] = str(output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    result = run(parser.parse_args().root)
    print(json.dumps({k: result[k] for k in ("status", "counts", "observations", "failures", "audit_output")}, ensure_ascii=False, indent=2))
