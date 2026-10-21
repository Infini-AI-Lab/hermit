"""Harness² under the paper's protocol, by the upstream code (`pip install -e '.[harness2]'`).

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
"""
from __future__ import annotations

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


def _upstream():
    try:
        from harness2.core import context, judge, library, schedule, spec
    except ImportError as exc:
        raise RuntimeError("the harness2_upstream Evolver needs the upstream package: pip install -e '.[harness2]'") \
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

    def setup(self, ctx) -> None:
        _upstream()
        if ctx.params.get("proposer") is None and ctx.params.get("backend") != "claude":
            raise ValueError("harness2_upstream runs its sessions as Claude Code: set params.backend: claude")

    def step(self, ctx) -> StepResult:
        context, judge, library, schedule, spec = _upstream()
        from hermit.algorithms.evolvers.harness2_upstream import surface as U
        from hermit.algorithms.evolvers.harness2_upstream.bench import FrameworkBench
        from hermit.algorithms.evolvers.harness2_upstream.channel import FrameworkProposer

        params, ws = ctx.params, ctx.workspace
        records = sorted((ws.learning_material / "current" / "tasks").glob("*.json"))
        if len(records) != 1:
            return StepResult("failed", f"the paper's protocol runs one task per step (data.step_size: 1); this step "
                                        f"has {len(records)}")
        record = json.loads(records[0].read_text())
        tid = record["id"]
        U.register()
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
                               sources=_load(params.get("sources"), ctx.base))
        prop = (_load(params.get("proposer"), ctx.base) or FrameworkProposer)(
            spec=channel, audit_dir=ws.output / "sessions", attempts=2,
            params={**params, "claude_bin": self._claude(ws)}, sandbox=self._sandbox(ctx), cancel=ctx.cancel)
        lib = library.Library(root=ws.state / "library" / MODE, domain=domain, surface=U.SURFACE)
        before = lib.entries()

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
        self._keep(root, ws.output / "h2")
        (ws.output / "report.md").write_text(self._report(tid, res, stages, selection, added))
        chosen = selection.get("chosen_label") or "nothing"
        return StepResult("done", f"{tid}: {res.status}; delivered {chosen}; library {len(before)} -> "
                                  f"{len(lib.entries())}", usage=list(getattr(prop, "usages", [])),
                          extra={"harness2": extra})

    # --- helpers --------------------------------------------------------------------------------------------------
    def _claude(self, ws) -> str:
        """The Claude Code binary, pinned for the whole run (it updates itself): its versioned path, kept in state."""
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
