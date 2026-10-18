"""ToolsmithEvolver: one model call per step rewrites the adapter's instructions (ADAPTER.md) and its tool bundles
(tools/<name>/) from the step's trajectories and the judge's feedback; the tools are then self-tested through the task's
confined runner (their own test, then one call through the same path the agent's harness uses) and the model gets one
repair turn with the exact failures. A tool that worked before and fails after the repair turn is restored.

Training State (state/): notes.jsonl — one line per step (summary, notices), shown at the next step; tests.json — the
last self-test of every tool (sha256, test, contract, working, restored_from_last_passing, notices); seed/ — the last
valid page the agent produced, the self-tests' workspace seed; images/ — the last self-test images when `images` > 0.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import time
import types
from typing import NamedTuple

from hermit.adapter.manifest import files_of, read_manifest, structure_of
from hermit.evolver.base import Evolver, StepContext, StepResult
from hermit.runtime import pngstats

HERE = Path(__file__).resolve().parent
CONTEXT, TOOLS = "ADAPTER.md", "tools"
NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
RESERVED = frozenset({"write_file", "read_file", "replace_text", "run_command", "inspect_page", "wm", "tool_catalog",
                      "tool_call", "context_package"})
PRIMITIVES = frozenset({"write_file", "read_file", "replace_text", "run_command", "tool_catalog", "tool_call"})
FILE_EXT = (".py", ".md", ".json", ".txt", ".js", ".cjs", ".mjs", ".sh")   # the template's bundle file kinds
FILE_VIEW_CHARS = 6000          # why: a tool file is shown whole with line numbers; beyond this only its head matters
MIN_IMAGE_BYTES = 1024          # why: a real capture is far larger; a 1 kB PNG is a stub or an empty canvas
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TAIL = 2000
NOTES_SHOWN = 5

_START = re.compile(r"^<<<FILE[ \t]+(.+?)[ \t]*>>>[ \t]*$")
_END = re.compile(r"^<<<END(?:[ \t]+FILE)?>>>[ \t]*$")
_DELETE = re.compile(r"^<<<DELETE[ \t]+(.+?)[ \t]*>>>[ \t]*$")
_SUMMARY = re.compile(r"^[ \t*_#>-]*SUMMARY:[ \t]*(.*)$")      # tolerant of a bold or quoted summary line
_BACKTICKED = re.compile(r"`([a-z][a-z0-9_]{0,63})(\(\))?`")
_FENCE = re.compile(r"^```[\w+-]*[ \t]*$")


class Reply(NamedTuple):
    files: list      # [(path, body)]
    deletes: list    # [path]
    summary: str | None
    problems: list   # what the grammar could not use


def parse_reply(text: str) -> Reply:
    """The file blocks, delete lines and SUMMARY of a reply. A block ends at <<<END FILE>>> (or <<<END>>>); a block
    with no end marker closes at the next block, delete line or SUMMARY, or at the end of the text."""
    files, deletes, problems = [], [], []
    summary = None
    current: tuple[str, list] | None = None

    def close(lines: list[str]) -> str:
        if len(lines) >= 2 and _FENCE.match(lines[0]) and _FENCE.match(lines[-1]):
            lines = lines[1:-1]      # why: models often wrap a file in a code fence; a file is never only a fence
        return "\n".join(lines)
    for line in text.splitlines():
        if current is not None:
            if _END.match(line):
                files.append((current[0], close(current[1])))
                current = None
                continue
            if _START.match(line) or _DELETE.match(line) or _SUMMARY.match(line):
                problems.append(f"block {current[0]} had no <<<END FILE>>> marker; closed at the next marker")
                files.append((current[0], close(current[1])))
                current = None
            else:
                current[1].append(line)
                continue
        start = _START.match(line)
        if start:
            current = (_strip_path(start.group(1)), [])
            continue
        delete = _DELETE.match(line)
        if delete:
            deletes.append(_strip_path(delete.group(1)))
            continue
        found = _SUMMARY.match(line)
        if found and summary is None:
            summary = found.group(1).strip(" *_\t") or None
    if current is not None:
        problems.append(f"block {current[0]} had no <<<END FILE>>> marker; closed at the end of the reply")
        files.append((current[0], close(current[1])))
    return Reply(files, deletes, summary, problems)


def _strip_path(raw: str) -> str:
    path = raw.strip().strip("`'\"")
    while path.startswith("./"):
        path = path[2:]
    return path


def check_path(rel: str, structure, body: str | None = None) -> str | None:
    """Why a reply path may not be written (None when it may): ADAPTER.md or tools/<name>/<file> only, no dot or
    parent components, the template's file kinds, the manifest's size limit for the learnable path."""
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return "not a relative path"
    parts = rel.split("/")
    if any(not p or p == ".." or p.startswith(".") for p in parts):
        return "no empty, parent or dot path components"
    if rel != CONTEXT:
        if len(parts) < 3 or parts[0] != TOOLS:
            return f"only {CONTEXT} or {TOOLS}/<name>/<file> may be written"
        if not NAME_RE.fullmatch(parts[1]):
            return f"tool directory {parts[1]!r} is not a tool name (^[a-z][a-z0-9_]{{0,63}}$)"
        if parts[1] in RESERVED:
            return f"{parts[1]!r} is a reserved name"
        if Path(parts[-1]).suffix not in FILE_EXT:
            return f"only {', '.join(FILE_EXT)} files belong in a bundle"
    item = structure.learnable_for(rel)
    if item is None:
        return "not a learnable path of this template"
    limit = item.format.get("max_bytes")
    if body is not None and limit is not None and len(body.encode("utf-8")) > int(limit):
        return f"{len(body.encode('utf-8'))} bytes exceeds max_bytes {limit} of `{item.path}`"
    return None


def lint_context(text: str, available: set[str]) -> list[str]:
    """Backticked identifiers that look like tool calls (`name()`, an underscore, or a line that speaks of a tool or a
    call) and are neither admitted tools nor primitives. A heuristic: the notice says which name does not exist."""
    unknown: list[str] = []
    for line in text.splitlines():
        speaks = re.search(r"\b(tools?|calls?|calling|invokes?|invoking)\b", line, re.IGNORECASE) is not None
        for name, parens in _BACKTICKED.findall(line):
            if name in available or name in unknown:
                continue
            if parens or "_" in name or speaks:
                unknown.append(name)
    return [f"{CONTEXT} mentions `{name}`, which is not an available tool (available: "
            f"{', '.join(sorted(available)) or 'none'})" for name in unknown]


def _clip(value, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + " …[truncated]"


def _tail(text: str, n: int = TAIL) -> str:
    return text if len(text) <= n else "…" + text[-n:]


def _load_sibling(name: str):
    spec = importlib.util.spec_from_file_location(f"_evolver_toolsmith_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def bundle_sha(directory: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            h.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


class ToolsmithEvolver(Evolver):
    def step(self, ctx: StepContext) -> StepResult:
        if "llm" not in ctx.tools:
            return StepResult("failed", "toolsmith needs the run's llm tool: set `llm` in run.yaml")
        p = ctx.params
        ws = ctx.workspace
        notices: list[str] = []
        runner_name = p.get("runner_tool")
        runner = ctx.tools.get(runner_name) if runner_name else None
        if not runner_name:
            notices.append("self-tests skipped: evolver.params.runner_tool is not set (no confined runner)")
        elif runner is None:
            notices.append(f"self-tests skipped: runner_tool {runner_name!r} is not among this run's tools")
        structure = structure_of(read_manifest(ws.adapter))
        iface = self._interface(ws.adapter, notices)
        previous = _read_json(ws.state / "tests.json", {})
        previous = previous if isinstance(previous, dict) else {}
        log = ws.output / "llm_replies"
        log.mkdir(parents=True, exist_ok=True)
        replies: list[str] = []
        tests: dict[str, dict] = {}
        changed: set[str] = set()
        applied: list[str] = []

        def ask(messages: list[dict]) -> Reply:
            reply = ctx.tools["llm"](messages=messages, max_tokens=int(p.get("max_tokens", 32000)),
                                     temperature=float(p.get("temperature", 0.3)))
            text = reply.get("content") or ""
            replies.append(text)
            (log / f"reply.{len(replies) - 1}.txt").write_text(text)
            messages.append({"role": "assistant", "content": text})
            parsed = parse_reply(text)
            notices.extend(f"reply {len(replies) - 1}: {problem}" for problem in parsed.problems)
            return parsed

        # 1–3. material, one model call (one retry when the reply holds nothing).
        user = self._material(ctx, structure, iface, previous, notices)
        system = (HERE / "prompt.md").read_text()
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._with_images(user, self._state_images(ws), p)}]
        reply = ask(messages)
        if not reply.files and not reply.deletes and reply.summary is None:
            messages.append({"role": "user", "content": "Your reply contained no file block and no SUMMARY line. "
                             "Reply now with the file blocks for the files you change (<<<FILE path>>> ... "
                             "<<<END FILE>>>), then the SUMMARY: line. No analysis."})
            reply = ask(messages)
        summary = reply.summary or "no summary"
        # 4. keep-last-passing snapshots, then apply.
        snapshots = self._snapshot(ws, previous)
        applied += self._apply(ws.adapter, structure, reply, changed, notices)
        # 5. self-tests, one repair turn, restore.
        if runner is not None and iface is not None:
            seed = self._seed(ctx)
            tests = self._test_round(ctx, iface, runner, seed, self._order(ws.adapter, changed, previous), notices)
            failures = {n: r for n, r in tests.items() if r.get("failed")}
            rounds = int(p.get("repair_rounds", 1))
            for _ in range(rounds):
                if not failures or (ctx.cancel is not None and ctx.cancel.is_set()):
                    break
                messages.append({"role": "user", "content": self._with_images(
                    self._repair_prompt(tests), self._test_images(ws, tests, p), p)})
                repair = ask(messages)
                summary = repair.summary or summary
                touched: set[str] = set()
                applied += self._apply(ws.adapter, structure, repair, touched, notices)
                changed |= touched
                retest = [n for n in self._order(ws.adapter, touched | set(failures), previous)
                          if n in touched or n in failures]
                tests.update(self._test_round(ctx, iface, runner, seed, retest, notices))
                failures = {n: r for n, r in tests.items() if r.get("failed")}
            self._restore(ws, snapshots, changed, tests, previous, notices)
            self._save_images(ws, tests, p)
        # ADAPTER.md lint against the catalog.
        admitted = set()
        if iface is not None:
            try:
                admitted_list, rejected = iface.catalog(ws.adapter)
                admitted = {t["name"] for t in admitted_list}
                limit = int(p.get("exposed_tools_max", 8))
                if len(admitted_list) > limit:
                    notices.append(f"{len(admitted_list)} admitted tools; the agent's harness exposes at most {limit}")
                notices += [f"tools/{r['name']} rejected: {r['problem']}" for r in rejected]
            except Exception as exc:     # a catalog error is reported, never fatal
                notices.append(f"catalog unavailable: {type(exc).__name__}: {exc}")
        context_path = ws.adapter / CONTEXT
        if context_path.is_file():
            notices += lint_context(context_path.read_text("utf-8", errors="replace"), admitted | PRIMITIVES)
        # 6. check, state, notes, report.
        check = ctx.tools["check_spec"]()
        if not check["passed"]:     # handed back as is: the Trainer repairs it and says so in the next step
            summary = "failed check_spec: " + "; ".join(i["problem"] for i in check["issues"][:3]) + f" — {summary}"
        self._write_tests(ws, tests, previous, changed)
        notes_file = ws.state / "notes.jsonl"
        notes_file.parent.mkdir(parents=True, exist_ok=True)
        with open(notes_file, "a") as f:
            f.write(json.dumps({"step": ctx.progress.step if ctx.progress else None, "summary": summary,
                                "notices": notices}, ensure_ascii=False) + "\n")
        (ws.output / "report.md").write_text(self._report(summary, applied, tests, notices, check))
        return StepResult("done", summary[:300], usage=[])     # usage of the llm tool is metered by the Trainer

    # --- the adapter's interface ------------------------------------------------------------------------------------
    @staticmethod
    def _interface(adapter: Path, notices: list[str]):
        """The adapter's fixed interface.py (catalog, program_argv) executed from source, as check.py does."""
        path = adapter / "interface.py"
        if not path.is_file():
            notices.append("self-tests skipped: the adapter has no interface.py (not a toolsmith adapter?)")
            return None
        module = types.ModuleType("_toolsmith_adapter_interface")
        module.__file__ = str(path)
        try:
            exec(compile(path.read_text("utf-8"), str(path), "exec"), module.__dict__)
        except Exception as exc:
            notices.append(f"self-tests skipped: interface.py does not load: {type(exc).__name__}: {exc}")
            return None
        if not callable(getattr(module, "catalog", None)) or not callable(getattr(module, "program_argv", None)):
            notices.append("self-tests skipped: interface.py lacks catalog/program_argv")
            return None
        return module

    # --- material -----------------------------------------------------------------------------------------------------
    def _material(self, ctx: StepContext, structure, iface, previous: dict, notices: list[str]) -> str:
        p, ws = ctx.params, ctx.workspace
        n = int(p.get("max_chars", 6000))
        current = ws.learning_material / "current"
        records = [r for r in (_read_json(f, None) for f in sorted((current / "tasks").glob("*.json")))
                   if isinstance(r, dict)]
        records.sort(key=lambda r: (r.get("judge") or {}).get("score") or 0.0)
        shown = records[: int(p.get("max_examples", 8))]
        digest = _load_sibling("digest")
        examples = []
        for r in shown:
            judge = r.get("judge") or {}
            block = (f"### Task {r.get('id')} — score {judge.get('score')}\n"
                     f"Input: {_clip(r.get('input'), n)}\n"
                     f"Agent's output: {_clip(r.get('output'), n)}\n"
                     f"Judge feedback: {_clip(judge.get('feedback', ''), n)}\n")
            if r.get("pipeline_extra"):
                block += f"Pipeline extra: {_clip(r['pipeline_extra'], n)}\n"
            trajectory = r.get("trajectory")
            if trajectory and (current / trajectory).is_dir():
                limit = int(p.get("trajectory_chars", 20000))
                block += "#### Trajectory\n" + digest.build(current / trajectory, limit) + "\n"
            examples.append(block)
        scores = [(r.get("judge") or {}).get("score") or 0.0 for r in records]
        mean = sum(scores) / max(len(scores), 1)
        feedback = current / "check_feedback.json"
        repaired = (f"\n## The Trainer had to repair your last version\n{_clip(feedback.read_text(), n)}\n"
                    if feedback.is_file() else "")
        notes_file = ws.state / "notes.jsonl"
        notes = notes_file.read_text().strip().splitlines()[-NOTES_SHOWN:] if notes_file.is_file() else []
        task_readme = ws.task_info / "README.md"
        environment = ws.task_info / "environment.md"
        guide = ws.adapter / "EVOLVER.md"
        parts = [f"## The task (as the acting agent receives it)\n"
                 f"{task_readme.read_text() if task_readme.is_file() else '(no description)'}\n"]
        if environment.is_file():
            parts.append(f"## Environment facts (the acting agent's machine)\n{environment.read_text()}\n")
        parts.append(f"## Your contract (EVOLVER.md)\n{guide.read_text() if guide.is_file() else '(none)'}\n")
        parts.append(self._adapter_view(ws.adapter, iface, notices))
        parts.append(f"## Results of this step (mean score {mean:.3f} on {len(records)} tasks)\n" + "\n".join(examples))
        parts.append(repaired)
        parts.append("## Self-tests of your tools (last step)\n"
                     + (self._format_tests(previous) or "(none yet)") + "\n")
        parts.append("## Notes from earlier rounds\n" + ("\n".join(notes) or "(none)") + "\n")
        parts.append("## Your reply\nImprove the adapter for the next attempts. Reply with file blocks for the files "
                     "you change (<<<FILE path>>> ... <<<END FILE>>>), <<<DELETE path>>> lines for what you remove, "
                     "then one line starting with SUMMARY: that says what you changed and why.")
        return "\n".join(parts)

    @staticmethod
    def _adapter_view(adapter: Path, iface, notices: list[str]) -> str:
        context = adapter / CONTEXT
        out = [f"## Current adapter\n### {CONTEXT}\n<<<\n{context.read_text() if context.is_file() else ''}\n>>>\n"]
        catalog = "(catalog unavailable)"
        if iface is not None:
            try:
                admitted, rejected = iface.catalog(adapter)
                catalog = "admitted: " + (", ".join(t["name"] for t in admitted) or "none")
                if rejected:
                    catalog += "; rejected: " + "; ".join(f"{r['name']} ({r['problem']})" for r in rejected)
            except Exception as exc:
                catalog = f"(catalog error: {type(exc).__name__}: {exc})"
        files = [rel for rel in files_of(adapter) if rel.startswith(TOOLS + "/") and rel != f"{TOOLS}/README.md"]
        out.append(f"### tools/ — {len(files)} files; {catalog}\n")
        for rel in files:
            text = (adapter / rel).read_text("utf-8", errors="replace")
            numbered = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(text.splitlines(), 1))
            if len(numbered) > FILE_VIEW_CHARS:
                more = len(numbered) - FILE_VIEW_CHARS
                numbered = numbered[:FILE_VIEW_CHARS] + f"\n…[clipped: {more} more characters]"
            out.append(f"#### {rel}\n{numbered}\n")
        return "\n".join(out)

    # --- apply ------------------------------------------------------------------------------------------------------
    @staticmethod
    def _apply(adapter: Path, structure, reply: Reply, changed: set[str], notices: list[str]) -> list[str]:
        """Write the blocks and delete the paths of a reply; the list of what was done. Invalid blocks are skipped and
        reported; a bundle's TOOL.json or its directory deleted removes the whole bundle."""
        done: list[str] = []
        max_files = next((int(i.format["max_files"]) for i in structure.learnable
                          if i.path.startswith(TOOLS) and i.format.get("max_files") is not None), None)
        for rel, body in reply.files:
            problem = check_path(rel, structure, body)
            if problem is None and max_files is not None and rel != CONTEXT and not (adapter / rel).exists():
                count = sum(1 for f in files_of(adapter)
                            if f.startswith(TOOLS) and structure.learnable_for(f) is not None)
                if count + 1 > max_files:
                    problem = f"would exceed max_files {max_files} under tools/"
            if problem:
                notices.append(f"skipped block {rel}: {problem}")
                continue
            target = adapter / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body if body.endswith("\n") or not body else body + "\n")
            done.append(f"wrote {rel}")
            if rel != CONTEXT:
                changed.add(rel.split("/")[1])
        for rel in reply.deletes:
            parts = rel.split("/")
            if rel == CONTEXT:
                notices.append(f"skipped delete {rel}: the context file cannot be deleted")
                continue
            problem = check_path(rel if len(parts) >= 3 else rel + "/TOOL.json", structure)
            if problem:
                notices.append(f"skipped delete {rel}: {problem}")
                continue
            bundle = adapter / TOOLS / parts[1]
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "TOOL.json"):
                if bundle.is_dir():
                    shutil.rmtree(bundle)
                    done.append(f"deleted bundle tools/{parts[1]}")
                    changed.add(parts[1])
                else:
                    notices.append(f"skipped delete {rel}: no such bundle")
                continue
            target = adapter / rel
            if not target.is_file():
                notices.append(f"skipped delete {rel}: no such file")
                continue
            target.unlink()
            done.append(f"deleted {rel}")
            changed.add(parts[1])
            for directory in target.parents:
                if directory == bundle.parent or not directory.is_relative_to(bundle.parent):
                    break
                try:
                    directory.rmdir()
                except OSError:
                    break
        return done

    # --- self-tests -------------------------------------------------------------------------------------------------
    @staticmethod
    def _order(adapter: Path, changed: set[str], previous: dict) -> list[str]:
        """Bundle directories to test: changed first, then never tested, then the rest; only existing directories."""
        tools = adapter / TOOLS
        names = sorted(d.name for d in tools.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))
                       and (d / "TOOL.json").is_file()) if tools.is_dir() else []
        key = lambda n: (0 if n in changed else 1 if n not in previous else 2, n)     # noqa: E731
        return sorted(names, key=key)

    def _seed(self, ctx: StepContext) -> dict[str, str]:
        """The self-test workspace's files: the best trajectory's valid index.html (else the last valid one seen, else
        the task README as index.html) and its README.md (else the task's)."""
        ws = ctx.workspace
        current = ws.learning_material / "current"
        records = [r for r in (_read_json(f, None) for f in sorted((current / "tasks").glob("*.json")))
                   if isinstance(r, dict)]
        records.sort(key=lambda r: -((r.get("judge") or {}).get("score") or 0.0))
        task_readme = ws.task_info / "README.md"
        readme = task_readme.read_text() if task_readme.is_file() else ""
        page = None
        for r in records:
            trajectory = r.get("trajectory")
            if not trajectory:
                continue
            d = current / trajectory
            index = d / "workspace" / "index.html"
            if not index.is_file():
                continue
            result = _read_json(d / "result.json", {})
            valid = result.get("valid_submission", True) if isinstance(result, dict) else True
            if (d / "workspace" / "README.md").is_file():
                readme = (d / "workspace" / "README.md").read_text("utf-8", errors="replace")
            if valid:
                page = index.read_text("utf-8", errors="replace")
                break
        seed_dir = ws.state / "seed"
        if page is not None:
            seed_dir.mkdir(parents=True, exist_ok=True)
            (seed_dir / "index.html").write_text(page)
        elif (seed_dir / "index.html").is_file():
            page = (seed_dir / "index.html").read_text("utf-8", errors="replace")
        else:
            page = readme      # why: no valid page yet — the task text gives a tool something to open and measure
        return {"index.html": page, "README.md": readme}

    def _test_round(self, ctx: StepContext, iface, runner, seed: dict[str, str], names: list[str],
                    notices: list[str]) -> dict[str, dict]:
        """Run the self-test of up to max_tests_per_round bundles: the bundle's own test (when declared), then the
        contract check through Session.call("tool_call") in a seeded scratch workspace. {name: record}."""
        p, ws = ctx.params, ctx.workspace
        per_test = float(p.get("test_timeout_s", 60))
        deadline = time.time() + float(p.get("test_total_s", 300))
        results: dict[str, dict] = {}
        admitted, rejected = iface.catalog(ws.adapter)
        bundles = {t["name"]: t for t in admitted}
        problems = {r["name"]: r["problem"] for r in rejected}
        step = ctx.progress.step if ctx.progress else None
        for name in names[: int(p.get("max_tests_per_round", 4))]:
            directory = ws.adapter / TOOLS / name
            record = {"sha256": bundle_sha(directory), "step": step, "test": None, "contract": None, "working": False,
                      "failed": True, "restored_from_last_passing": False, "notices": []}
            results[name] = record
            if name in problems:
                record["contract"] = {"ok": False, "problem": f"rejected by the catalog: {problems[name]}"}
                record["notices"].append(record["contract"]["problem"])
                continue
            if ctx.cancel is not None and ctx.cancel.is_set():
                record["notices"].append("not tested: the step was cancelled")
                continue
            if time.time() > deadline:
                record["notices"].append("not tested: the self-test time budget of this round is spent")
                continue
            bundle = bundles[name]
            workspace = ws.scratch / "ws" / name
            if workspace.exists():
                shutil.rmtree(workspace)
            workspace.mkdir(parents=True)
            for file, text in seed.items():
                (workspace / file).write_text(text)

            def delegate(**kw):
                kw["timeout_s"] = min(float(kw.get("timeout_s") or per_test), per_test)
                out = runner(**kw)
                return out if isinstance(out, dict) else {}
            if bundle.get("test"):
                record["test"] = self._run_test(iface, delegate, directory, bundle["test"], str(workspace))
            if time.time() <= deadline:
                record["contract"] = self._contract_check(ws.adapter, delegate, workspace, name, bundle)
            else:
                record["contract"] = {"ok": False,
                                      "problem": "not called: the self-test time budget of this round is spent"}
            contract = record["contract"]
            images = contract.get("images") or []
            nonblank = any(not i.get("blank", True) for i in images)
            record["working"] = bool(contract.get("ok")) and nonblank
            test_failed = record["test"] is not None and not record["test"]["passed"]
            record["failed"] = test_failed or not contract.get("ok") or (bool(images) and not nonblank)
            if images and not nonblank and contract.get("ok"):
                record["notices"].append("every image is blank (uniform or too small): nothing to look at")
        return results

    @staticmethod
    def _run_test(iface, delegate, directory: Path, test: str, cwd: str) -> dict:
        try:
            argv = iface.program_argv(test, {})
            run = delegate(argv=argv, cwd=cwd, timeout_s=None, ro=[[str(directory), iface.MOUNT]], lock="shared")
        except Exception as exc:
            return {"passed": False, "exit_code": None, "seconds": None, "timed_out": False,
                    "stdout_tail": "", "stderr_tail": f"{type(exc).__name__}: {exc}", "error": str(exc)}
        passed = run.get("exit_code") == 0 and not run.get("timed_out") and not run.get("error")
        return {"passed": passed, "exit_code": run.get("exit_code"), "seconds": run.get("seconds"),
                "timed_out": bool(run.get("timed_out")), "stdout_tail": _tail(str(run.get("stdout") or "")),
                "stderr_tail": _tail(str(run.get("stderr") or "")), "error": run.get("error")}

    @staticmethod
    def _contract_check(adapter: Path, delegate, workspace: Path, name: str, bundle: dict) -> dict:
        """One call through the path the agent's harness uses; the result's text, images and their measures."""
        from hermit.adapter.session import Session
        args = bundle.get("example_args") or {}
        try:
            session = Session(adapter, resources={"tool_runner": delegate, "workspace": str(workspace)})
            result = session.call("tool_call", {"name": name, "args": args})
        except Exception as exc:
            return {"ok": False, "problem": f"tool_call raised {type(exc).__name__}: {exc}", "args": args}
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            return {"ok": False, "problem": "the result is not an object with a text string", "args": args}
        out = {"ok": True, "problem": None, "args": args, "text": _clip(result["text"], 600),
               "exit_code": result.get("exit_code"), "seconds": result.get("seconds"),
               "timed_out": bool(result.get("timed_out")), "stderr_tail": _tail(str(result.get("stderr_tail") or "")),
               "error": result.get("error"), "images": [], "images_rejected": result.get("images_rejected") or [],
               "diff_first_last": None}
        if result.get("error"):
            out.update(ok=False, problem=f"the call failed ({result['error']}): {_clip(result['text'], 1500)}")
        for rel in result.get("images") or []:
            path = workspace / rel
            info = {"file": rel, "bytes": path.stat().st_size if path.is_file() else 0, "blank": True, "problem": None}
            head = path.read_bytes()[:8] if path.is_file() else b""
            if not head.startswith(PNG_MAGIC):
                info["problem"] = "not a PNG file"
            elif info["bytes"] <= MIN_IMAGE_BYTES:
                info["problem"] = f"only {info['bytes']} bytes"
            else:
                stats = pngstats.stats(path)
                info.update({k: stats[k] for k in ("width", "height", "mean_luma", "nonuniform_fraction", "blank")})
                if stats["error"]:
                    info["problem"] = f"does not decode: {stats['error']}"
            out["images"].append(info)
        bad = [f"{i['file']}: {i['problem']}" for i in out["images"] if i["problem"]]
        bad += [f"{r.get('path')}: {r.get('problem')}" for r in out["images_rejected"] if isinstance(r, dict)]
        if bad and out["ok"]:
            out.update(ok=False, problem="images not usable: " + "; ".join(bad))
        decodable = [workspace / i["file"] for i in out["images"] if not i["problem"]]
        if len(decodable) >= 2:
            out["diff_first_last"] = pngstats.diff_fraction(decodable[0], decodable[-1])
        return out

    # --- repair, restore, images ------------------------------------------------------------------------------------
    def _repair_prompt(self, tests: dict[str, dict]) -> str:
        return ("## Self-test results of your new version\n" + self._format_tests(tests) +
                "\n\nFix the failing tools (or delete a tool you cannot fix). The tools ran on the acting agent's "
                "machine as the task README describes it; the error output above is exact. Reply with file blocks for "
                "the files "
                "you change only, then the SUMMARY: line.")

    @staticmethod
    def _format_tests(tests: dict[str, dict]) -> str:
        out = []
        for name, r in tests.items():
            state = "yes" if r.get("working") else "no"
            out.append(f"### tools/{name} — working: {state}" + (" (restored from the last passing version)"
                                                                 if r.get("restored_from_last_passing") else ""))
            t = r.get("test")
            if t is None:
                out.append("test: none declared")
            else:
                out.append(f"test: {'passed' if t['passed'] else 'FAILED'} (exit {t['exit_code']}, {t['seconds']} s"
                           + (", timed out" if t["timed_out"] else "")
                           + (f", {t['error']}" if t.get("error") else "") + ")")
                if not t["passed"]:
                    if t["stdout_tail"]:
                        out.append("  stdout tail:\n    " + t["stdout_tail"].replace("\n", "\n    "))
                    if t["stderr_tail"]:
                        out.append("  stderr tail:\n    " + t["stderr_tail"].replace("\n", "\n    "))
            c = r.get("contract")
            if c is None:
                out.append("contract call: not run")
            elif c.get("ok"):
                out.append(f"contract call with {json.dumps(c.get('args', {}))}: ok (exit {c.get('exit_code')}, "
                           f"{c.get('seconds')} s); text: {c.get('text', '')!r}")
            else:
                out.append(f"contract call with {json.dumps(c.get('args', {}))}: FAILED — {c.get('problem')}")
                if c.get("stderr_tail"):
                    out.append("  stderr tail:\n    " + c["stderr_tail"].replace("\n", "\n    "))
            if c and c.get("images") is not None:
                images = c["images"]
                if images:
                    parts = []
                    for i in images:
                        if i.get("problem"):
                            parts.append(f"{i['file']} ({i['problem']})")
                        else:
                            parts.append(f"{i['file']} {i.get('width')}x{i.get('height')} luma {i.get('mean_luma')} "
                                         f"nonuniform {i.get('nonuniform_fraction')}"
                                         + (" BLANK" if i.get("blank") else ""))
                    out.append(f"images: {len(images)} — " + "; ".join(parts)
                               + (f"; change first→last {c['diff_first_last']}"
                                  if c.get("diff_first_last") is not None else ""))
                else:
                    out.append("images: none")
            for note in r.get("notices") or []:
                out.append(f"note: {note}")
            out.append("")
        return "\n".join(out).strip()

    @staticmethod
    def _snapshot(ws, previous: dict) -> dict[str, Path]:
        """Copies of every bundle whose last self-test said working, taken before anything is applied."""
        snapshots = {}
        for name, record in previous.items():
            source = ws.adapter / TOOLS / name
            if isinstance(record, dict) and record.get("working") and source.is_dir():
                target = ws.scratch / "snapshots" / name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
                snapshots[name] = target
        return snapshots

    @staticmethod
    def _restore(ws, snapshots: dict[str, Path], changed: set[str], tests: dict[str, dict], previous: dict,
                 notices: list[str]) -> None:
        """Keep-last-passing: a bundle that worked before, was changed this step and does not work now goes back."""
        for name, snapshot in snapshots.items():
            if name not in changed:
                continue
            record = tests.get(name)
            if record is None:
                if (ws.adapter / TOOLS / name).is_dir():
                    notices.append(f"tools/{name} was changed but not tested this step (time budget); kept as is")
                continue      # deleted on purpose, or untested: not restored
            if record.get("working"):
                continue
            target = ws.adapter / TOOLS / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(snapshot, target)
            tests[name] = {**previous[name], "restored_from_last_passing": True, "failed": False,
                           "notices": [f"the new version failed its self-test; restored the last passing version"]
                           + list(record.get("notices") or []),
                           "last_failure": (record.get("contract") or {}).get("problem")}
            notices.append(f"tools/{name}: the new version failed its self-test; restored the last passing version")

    @staticmethod
    def _write_tests(ws, tests: dict[str, dict], previous: dict, changed: set[str]) -> None:
        state: dict[str, dict] = {}
        tools = ws.adapter / TOOLS
        for name, record in previous.items():
            if (tools / name).is_dir() and name not in tests and isinstance(record, dict):
                stale = record.get("sha256") != bundle_sha(tools / name)
                state[name] = {**record, "stale": stale}
        for name, record in tests.items():
            if (tools / name).is_dir():
                state[name] = {k: v for k, v in record.items() if k != "failed"}
        ws.state.mkdir(parents=True, exist_ok=True)
        (ws.state / "tests.json").write_text(json.dumps(state, indent=1, ensure_ascii=False, default=str))

    @staticmethod
    def _test_images(ws, tests: dict[str, dict], p: dict) -> list[Path]:
        limit = int(p.get("images", 0) or 0)
        paths: list[Path] = []
        for name, record in tests.items():
            for info in (record.get("contract") or {}).get("images") or []:
                if not info.get("problem"):
                    paths.append(ws.scratch / "ws" / name / info["file"])
        return paths[:limit]

    @staticmethod
    def _state_images(ws) -> list[Path]:
        directory = ws.state / "images"
        return sorted(directory.glob("*.png")) if directory.is_dir() else []

    def _save_images(self, ws, tests: dict[str, dict], p: dict) -> None:
        directory = ws.state / "images"
        if directory.is_dir():
            shutil.rmtree(directory)
        paths = self._test_images(ws, tests, p)
        if paths:
            directory.mkdir(parents=True)
            for i, path in enumerate(paths):
                if path.is_file():
                    shutil.copy2(path, directory / f"{i:02d}-{path.parent.name}-{path.name}")

    @staticmethod
    def _with_images(text: str, paths: list[Path], p: dict):
        """The user content: plain text, or text plus image_url parts when `images` > 0 and there are images."""
        limit = int(p.get("images", 0) or 0)
        parts = [{"type": "text", "text": text}]
        for path in paths[:limit]:
            if path.is_file():
                data = base64.b64encode(path.read_bytes()).decode("ascii")
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
        return text if len(parts) == 1 else parts

    # --- report -----------------------------------------------------------------------------------------------------
    def _report(self, summary: str, applied: list[str], tests: dict[str, dict], notices: list[str], check: dict) -> str:
        lines = [summary, "", "## Applied", *(f"- {a}" for a in applied or ["(nothing)"]), ""]
        if tests:
            lines += ["## Self-tests", self._format_tests(tests), ""]
        if notices:
            lines += ["## Notices", *(f"- {n}" for n in notices), ""]
        if not check["passed"]:
            lines += ["## check_spec", *(f"- {i['path']}: {i['problem']}" for i in check["issues"]), ""]
        return "\n".join(lines)
