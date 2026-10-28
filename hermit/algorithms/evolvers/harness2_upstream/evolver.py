"""Harness² under the paper's protocol, by the upstream code (`python -m pip install -e . --group harness2`).

The paper (Xu et al., arXiv:2610.14224, Sec. 2) refines a harness per task, at test time: each task of a stream
starts from the common harness H0; a reference execution; an INV and a DOM editor session, each candidate executed on
the same task; a composer session whose composed harness is executed on the same task, and that execution is the
task's deliverable; a reflection that stores the adopted harness and its lesson in the domain's library, which is all
the next task inherits. The training split is that stream: `data.step_size: 1`, `data.shuffle: false`, one epoch.

Each step hands upstream `harness2.core.schedule.run_task` the step's task through `bench.FrameworkBench` (executions
by the run_agent tool; the reference execution is the Trainer's rollout of the step, H0 on this task) and
`channel.FrameworkProposer` (upstream's claude_cli sessions on the bound model, sandboxed). The adapter is never
written, so every Trainer rollout is H0; the library lives in the Training State. The step's records go to its output
(the per-task tree upstream writes, without the copied source files); the scores of its executions, which no session
sees, go to the step result (`extra.harness2`) for the experimenter.

`params.library_from` continues a finished stream: its library is copied into the Training State before the first
task, so the new tasks come after that stream's (upstream: a task sees the entries of every task before it). The editor
settings must be that stream's, and a task it took is refused.
"""
from __future__ import annotations

import filecmp
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import yaml

from hermit.evolver.base import Evolver, StepResult

MODE = "agg"           # parallel recursion; at k = 1 the sequential mode is the same procedure (upstream report.py)
# The editor settings a continuation (library_from) shares with the stream it continues; slots, sources and the
# Claude Code binary are compared by content.
SAME = ("backend", "model", "effort", "k", "session_timeout_s", "tools", "domain_field", "name", "obs_trunc",
        "leak_globs", "verdict_markers", "price")


def _upstream():
    try:
        from harness2.core import context, judge, library, schedule, spec
    except ImportError as exc:
        raise RuntimeError("the harness2_upstream Evolver needs the upstream package: python -m pip install -e . --group harness2 (from the Hermit checkout root; requires pip >=25.1)") \
            from exc
    return context, judge, library, schedule, spec


def _load(ref: str | None, base: Path | None):
    """`file.py:name` (relative to the run config) or `module:name`, or None."""
    if not ref:
        return None
    module_part, _, attr = str(ref).partition(":")
    if module_part.endswith(".py"):
        path = (Path(base or ".") / module_part).resolve()
        spec = importlib.util.spec_from_file_location(f"_h2u_{path.stem}_{abs(hash(path))}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(module_part)
    return getattr(module, attr)


def _path(value, base: Path | None) -> Path:
    """A path parameter resolved as the run config resolves paths: variables and ~ expanded, relative to the config."""
    path = Path(os.path.expandvars(str(value))).expanduser()
    return (path if path.is_absolute() else Path(base or ".") / path).resolve()


def _markers(context, extra: list[str]) -> None:
    """Add this task's grader wording to upstream's leak tripwire (it checks every evidence tree and harness)."""
    extra = tuple(str(m) for m in extra or () if str(m))
    if not extra or getattr(context, "_mha_markers", None) == extra:
        return
    markers = tuple(context.GRADER_VERDICT_MARKERS) + extra
    tree_check = getattr(context, "_mha_tree_check", context.assert_tree_has_no_grader_verdicts)
    dir_check = getattr(context, "_mha_dir_check", context.assert_no_grader_verdicts)
    context._mha_tree_check, context._mha_dir_check, context._mha_markers = tree_check, dir_check, extra
    context.assert_tree_has_no_grader_verdicts = lambda tree, markers=markers: tree_check(tree, markers)
    context.assert_no_grader_verdicts = lambda root, markers=markers: dir_check(root, markers)


class Harness2UpstreamEvolver(Evolver):
    _pinned_bin = ""

    def setup(self, ctx) -> None:
        context = _upstream()[0]
        if ctx.params.get("proposer") is None and ctx.params.get("backend") != "claude":
            raise ValueError("harness2_upstream runs its sessions as Claude Code: set params.backend: claude")
        if ctx.params.get("library_from"):
            _markers(context, ctx.params.get("verdict_markers"))
            self._import_library(_path(ctx.params["library_from"], ctx.base), ctx.workspace, ctx.params, ctx.base,
                                 context)

    def step(self, ctx) -> StepResult:
        context, judge, library, schedule, spec = _upstream()
        from hermit.algorithms.evolvers.harness2_upstream import surface as U
        from hermit.algorithms.evolvers.harness2_upstream.bench import FrameworkBench
        from hermit.algorithms.evolvers.harness2_upstream.channel import FrameworkProposer

        params, ws = ctx.params, ctx.workspace
        self._pinned_bin = str(params.get("claude_bin") or "")
        records = sorted((ws.learning_material / "current" / "tasks").glob("*.json"))
        if len(records) != 1:
            return StepResult("failed", f"the paper's protocol runs one task per step (data.step_size: 1); this step "
                                        f"has {len(records)}")
        record = json.loads(records[0].read_text())
        tid = record["id"]
        layout = U.layout_of(ws.adapter)
        layout.register()
        _markers(context, params.get("verdict_markers"))
        slots = yaml.safe_load(Path(ctx.base or ".", params["slots"]).read_text()) if params.get("slots") else {}
        prompts = spec.BenchPrompts(**{k: str(v).strip() for k, v in (slots or {}).items()})
        k = int(params.get("k") or 1)
        model = str(params.get("model") or "")
        channel = spec.ChannelSpec("claude_cli", model, effort=str(params.get("effort") or "medium"),
                                   timeout=int(params.get("session_timeout_s") or 3600))
        domain = str((record.get("input") or {}).get(params.get("domain_field") or "domain") or "default")
        root = ws.scratch / "h2"
        bench = FrameworkBench(ctx, record, prompts=prompts, channel=channel, runs_root=root, domain=domain,
                               name=str(params.get("name") or "task"), obs_trunc=int(params.get("obs_trunc") or 50000),
                               leak_globs=tuple(params.get("leak_globs") or ()),
                               sources=_load(params.get("sources"), ctx.base), layout=layout)
        sandbox, claude = self._sandbox(ctx), self._claude(ws)
        if sandbox is not None:
            self._assert_hidden(sandbox, claude, [ws.learning_material.parent, *(params.get("probe_absent") or [])])
        prop = (_load(params.get("proposer"), ctx.base) or FrameworkProposer)(
            spec=channel, audit_dir=ws.output / "sessions", attempts=2,
            params={**params, "claude_bin": claude}, sandbox=sandbox, cancel=ctx.cancel)
        lib = library.Library(root=ws.state / "library" / MODE, domain=domain, surface=layout.surface)
        imported = ws.state / "library" / "imported.json"
        if params.get("library_from") and not imported.is_file():
            return StepResult("failed", "library_from is applied when a run starts, and this run started without it: "
                                        "start a new run")
        if imported.is_file() and library.slug(tid) in json.loads(imported.read_text())["taken"]:
            return StepResult("failed", f"{tid} is a task of the stream this run continues (library_from): a "
                                        f"continuation takes other tasks")
        lib.forget([tid])                    # an entry from an attempt at this task that never finished (upstream retain)
        before = lib.entries()
        down = self._endpoint_down(params, ctx.cancel)
        if down:
            return StepResult("failed", f"{tid}: the editors' model endpoint is not answering ({down})")

        res = schedule.run_task(tid, k, adapter=bench, prop=prop, runs_root=root, modes=(MODE,), library=lib,
                                selector_prop=prop)

        ns = str(schedule.ns(bench.spec.name, bench.substrate.name, MODE, k))
        summary = json.loads((root / ns / tid.replace("/", "__") / "summary.json").read_text())
        selection = (summary.get("selections") or {}).get(MODE) or {}
        row = judge.judge_selections(bench, [selection], parallel=1)[0] if selection else None
        runs = {}
        for run_id, judged in bench.private.items():
            run_dir = root / "executions" / run_id
            runs[run_id.rsplit("/", 1)[-1]] = {"answered": bench.delivered(run_dir), "score": judged.get("score")}
        finals = [f"agg{i}" for i in range(1, k + 1)]
        added = sorted(set(lib.entries()) - set(before))
        mode_rec = (summary.get("modes") or {}).get(MODE) or {}
        stages = [{key: s.get(key) for key in ("label", "dim", "fell_back", "kept_base", "attempts", "usd", "error")}
                  for s in mode_rec.get("stages") or []]
        extra = {"task": tid, "domain": domain, "status": res.status, "errors": res.errors, "stages": stages,
                 "selection": {key: selection.get(key) for key in ("ok", "trivial", "chosen_label", "chosen_is_base",
                                                                   "error", "fallback")},
                 "library": {"entries_before": len(before), "added": added},
                 "runs": runs, "finals": finals,
                 "judged": None if row is None else {"graded": row.graded, "frac": row.frac,
                                                     "label": row.label, "error": row.error},
                 "sessions_usd": round(sum(u.cost_usd or 0.0 for u in getattr(prop, "usages", [])), 6),
                 "claude": params.get("claude_bin") or self._claude(ws)}
        # Every editor stage fell back without a session reply: the model endpoint failed, and the deliverable is H0's.
        extra["editors_failed"] = bool(stages) and all(s["fell_back"] for s in stages) \
            and not getattr(prop, "usages", [])
        self._keep(root, ws.output / "h2")
        (ws.output / "harness2.json").write_text(json.dumps(extra, indent=1, default=str))
        (ws.output / "report.md").write_text(self._report(tid, res, stages, selection, added))
        chosen = selection.get("chosen_label") or "nothing"
        status = "failed" if extra["editors_failed"] else "done"
        return StepResult(status, f"{tid}: {res.status}; delivered {chosen}; library {len(before)} -> "
                                  f"{len(lib.entries())}" + ("; NO EDITOR SESSION RAN" if status == "failed" else ""),
                          usage=list(getattr(prop, "usages", [])), extra={"harness2": extra})

    # --- helpers --------------------------------------------------------------------------------------------------
    @staticmethod
    def _endpoint_down(params: dict, cancel, wait_s: int = 600) -> str:
        """For a model bound to a local relay (a wire meter), wait up to `wait_s` for it to answer; the reason it does
        not, or "" (also for a remote endpoint, which is not checked)."""
        import time
        import urllib.request
        from urllib.parse import urlsplit
        url = str((params.get("binding") or {}).get("base_url") or "")
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost"):
            return ""
        health, reason, started = f"{parts.scheme}://{parts.netloc}/health", "", time.time()
        while time.time() - started < wait_s and not (cancel is not None and cancel.is_set()):
            try:
                with urllib.request.urlopen(health, timeout=5):
                    return ""
            except OSError as exc:
                reason = f"{health}: {exc}"
            time.sleep(10)
        return reason or "stopped"

    def _claude(self, ws) -> str:
        """The Claude Code binary, pinned for the whole run (it updates itself): `params.claude_bin` (a copy the
        launcher made), else its versioned path, kept in state."""
        if self._pinned_bin:
            return self._pinned_bin
        pin = ws.state / "claude_bin.txt"
        if pin.is_file() and Path(pin.read_text().strip()).is_file():
            return pin.read_text().strip()
        found = shutil.which("claude")
        path = os.path.realpath(found) if found else "claude"
        pin.write_text(path + "\n")
        return path

    @staticmethod
    def _sandbox(ctx):
        """A sandbox of its own for the sessions: the system, the Claude install and the session directory only, the
        network only through the run's allowlist proxy (or a forwarded 127.0.0.1 model endpoint). None when the run
        has no sandbox."""
        run = ctx.sandbox
        if run is None or not getattr(run, "enabled", False):
            return None
        from hermit.sandbox import Sandbox
        absent = [Path(p).expanduser() for p in ctx.params.get("probe_absent") or []]
        return Sandbox(enabled=True, network="isolated", allow=list(getattr(run, "allow", []) or []),
                       proxy_url=getattr(run, "proxy_url", None), env_allow=[], enforce=getattr(run, "enforce",
                                                                                                 "required"),
                       record_dir=ctx.workspace.output / "sandbox", probe_absent=absent, role="editor")

    @staticmethod
    def _assert_hidden(sandbox, program: str, paths) -> None:
        """Refuse to start sessions whose sandbox would show the run (its learning material and tools log carry the
        judge's feedback, its output the scores) or an answer file: a bound directory above one of them shows it."""
        shown = [Path(p).resolve() for p in sandbox.visible([program])]
        for path in (Path(p).expanduser().resolve() for p in paths):
            for bound in shown:
                if bound == path or bound in path.parents:
                    raise RuntimeError(f"the editors' sandbox would show {path} (it binds {bound}); move what is "
                                       f"bound out of the way, e.g. a pinned Claude Code binary into a directory of "
                                       f"its own")

    @staticmethod
    def _import_library(source: Path, ws, params: dict, base: Path | None, context) -> None:
        """Copy the library of the stream at `source` (its run directory, or a library's mode directory) into the
        Training State, with a record (state/library/imported.json) of where it came from and the tasks that stream
        took. Refuses a run without summary.json (still going, or interrupted), this run itself, editor settings that
        differ from that run's, a library with links and an entry carrying grader wording (upstream's tripwire, as when
        the entry was added). Anything already in the state is from a start of this run that recorded no step: it is
        replaced."""
        from harness2.core.library import slug
        if (source / "workspace").is_dir():
            run = source
        else:                                # a directory inside a run's workspace is that run's
            run = next((p.parent for p in (source, *source.parents)
                        if p.name == "workspace" and (p.parent / "config.yaml").is_file()), None)
        root = run / "workspace" / "state" / "library" / MODE if run else source
        taken, stop = [], None
        if run:
            if run.resolve() == ws.state.parent.parent.resolve():
                raise RuntimeError(f"library_from: {run} is this run")
            if not (run / "summary.json").is_file():
                raise RuntimeError(f"library_from: the run {run} has not finished")
            stop = json.loads((run / "summary.json").read_text()).get("stop_reason")
            steps = (run / "steps.jsonl").read_text().splitlines() if (run / "steps.jsonl").is_file() else []
            taken = [slug(t) for line in steps if line.strip() for t in json.loads(line).get("tasks") or []]
            Harness2UpstreamEvolver._same_settings(run, params, base)
        domains = sorted(d for d in root.iterdir() if (d / "INDEX.md").is_file()) if root.is_dir() else []
        if not domains:
            raise RuntimeError(f"library_from: no library in {root}")
        links = [p for p in root.rglob("*") if p.is_symlink()]
        if links:
            raise RuntimeError(f"library_from: {links[0]} is a link")
        entries = []
        for d in domains:
            for entry in sorted(p for p in (d / "harnesses").glob("*") if p.is_dir()):
                context.assert_tree_has_no_grader_verdicts(
                    {str(f.relative_to(entry)): f.read_text(errors="replace") for f in entry.rglob("*") if f.is_file()})
                entries.append(f"{d.name}/{entry.name}")
        dest = ws.state / "library"
        shutil.rmtree(dest / MODE, ignore_errors=True)
        shutil.copytree(root, dest / MODE)
        taken = sorted(set(taken) | {e.split("/", 1)[1] for e in entries})
        (dest / "imported.json").write_text(json.dumps({"from": str(root), "stop_reason": stop, "entries": entries,
                                                        "taken": taken}, indent=1) + "\n")

    @staticmethod
    def _same_settings(run: Path, params: dict, base: Path | None) -> None:
        """Refuse editor settings that differ from those of the run at `run` (its config.yaml and pinned binary)."""
        recorded = yaml.safe_load((run / "config.yaml").read_text()) or {}
        theirs = (recorded.get("evolver") or {}).get("params") or {}
        out = Path(str(((recorded.get("config") or {}).get("trainer") or {}).get("output_dir") or "runs"))
        their_base = None if out.is_absolute() else run.parents[len(out.parts)]
        differ = [k for k in SAME if theirs.get(k) != params.get(k)]
        for key in ("slots", "sources"):
            mine, other = (str(v or "").partition(":")[0] for v in (params.get(key), theirs.get(key)))
            if not mine and not other:
                continue
            a = _path(mine, base) if mine else None
            b = _path(other, their_base) if other and (their_base or Path(other).is_absolute()) else None
            if not (a and b and a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)):
                differ.append(key)
        pin = run / "workspace" / "state" / "claude_bin.txt"
        mine = params.get("claude_bin") or os.path.realpath(shutil.which("claude") or "claude")
        other = theirs.get("claude_bin") or (pin.read_text().strip() if pin.is_file() else "")
        if mine != other and not (Path(mine).is_file() and Path(other).is_file()
                                  and filecmp.cmp(mine, other, shallow=False)):
            differ.append("claude_bin")
        if differ:
            raise RuntimeError(f"library_from: {', '.join(differ)} differ from the stream it continues ({run})")

    @staticmethod
    def _keep(root: Path, dest: Path) -> None:
        """The step's upstream records, without the copied source material (it is the task's, and large)."""
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(root, dest, symlinks=True,
                        ignore=lambda d, names: [n for n in names if n in ("source", "_sources", "_validate")])

    @staticmethod
    def _report(tid: str, res, stages: list[dict], selection: dict, added: list[str]) -> str:
        lines = [f"# {tid}: {res.status}", ""]
        for s in stages:
            how = "fell back to H0" if s["fell_back"] else "kept H0" if s["kept_base"] else "edited"
            lines.append(f"- {s['label']} ({s['dim']}): {how}, {s['attempts']} attempt(s)"
                         + (f"; {s['error']}" if s.get("error") else ""))
        lines.append(f"- delivered: {selection.get('chosen_label') or 'nothing'}"
                     + (f" ({selection.get('error')})" if selection.get("error") else ""))
        lines.append(f"- library: {'added ' + ', '.join(added) if added else 'no new entry'}")
        if res.errors:
            lines += ["", "Errors:", *[f"- {e}" for e in res.errors]]
        return "\n".join(lines) + "\n"
