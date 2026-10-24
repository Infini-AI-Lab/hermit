# Derived from google-research/harness2 core/context.py, Copyright 2026 Google LLC, Apache-2.0.
"""The evidence tree an improver session reads. The only way evidence reaches a model.

    context/task.md                      the step's task(s) as the agent received them
    context/tasks/<id>.md                one file per task (batches)
    context/source/<id>/                 task source material, when the task has files (answer keys excluded)
    context/rollouts/<label>.md          FULL trajectories of the reference set, one per task
    context/deliverables/<label>.md      what each reference rollout produced
    context/harness/<rel>                the component tree being edited
    context/library/                     earlier entries of this domain (INDEX.md first)
    context/pool/<label>/                a candidate: harness/, harness.diff, rollouts/<id>.md, produced/<id>.md
    context/decisions/<label>.md         earlier stages' notes
    MISSION.md                           what to do with all of it

Evidence is served as files and the prompt is a short pointer, so nothing is truncated to fit a prompt. The llm
channel inlines these files under a size budget; the agent channel mounts the directory.

Label-free by construction: the builders never receive judge output. The tripwires below refuse a tree that
carries grader verdicts or answer-key paths anyway; if one fires, find the read path that leaked, do not widen it.
"""
from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from . import surface as S


@dataclass
class Rollout:
    """What the method knows about one finished rollout. Never carries a score."""
    label: str
    task_id: str
    trajectory: str
    deliverable: str
    ok: bool = True
    delivered: bool = True
    error: str = ""


@dataclass
class Evidence:
    root: Path
    tasks: list[str]
    rollouts: list[str]
    harness_files: int = 0
    library: bool = False
    pool: list[str] = field(default_factory=list)
    decisions: int = 0
    source_files: int = 0
    failed: list[str] = field(default_factory=list)
    extras: list[tuple[str, str]] = field(default_factory=list)     # (path under context/, what it is)

    @property
    def layout(self) -> str:
        lines = ["context/task.md                  the task(s) as the agent received them"]
        for rel, what in self.extras:
            lines.append(f"context/{rel:<25}{what}")
        if len(self.tasks) > 1:
            lines.append("context/tasks/<id>.md            one file per task of this step")
        if self.source_files:
            lines.append("context/source/<id>/             task source materials")
        if self.decisions:
            lines.append("context/decisions/<label>.md     earlier edits and composition decisions")
        if self.rollouts:
            names = ", ".join(f"{r}.md" for r in self.rollouts[:6])
            more = f" (+{len(self.rollouts) - 6} more)" if len(self.rollouts) > 6 else ""
            lines.append(f"context/rollouts/                FULL trajectories: {names}{more}")
            lines.append("context/deliverables/<label>.md  what that rollout actually produced")
        if self.harness_files:
            lines.append(f"context/harness/                 the harness you are editing ({self.harness_files} files)")
        if self.pool:
            lines.append(f"context/pool/<label>/            {len(self.pool)} candidates, each with its harness, "
                         f"harness.diff, rollouts/ and produced/")
        if self.library:
            lines.append("context/library/                 earlier tasks in this area -- browse INDEX.md first, "
                         "then open what looks relevant")
        return "\n".join(lines)


def build(dest: Path | str, *, tasks: Mapping[str, str], rollouts: Sequence[Rollout] = (),
          harness_dir: Path | None = None, library_dir: Path | None = None,
          pool: Sequence[tuple[str, Path, Sequence[Rollout]]] = (), decisions: Mapping[str, str] | None = None,
          source: Mapping[str, Path] | None = None, leak_globs: Sequence[str] = (),
          surface: S.SurfaceSpec | None = None,
          extra: Sequence[tuple[str, str | Path, str]] = ()) -> Evidence:
    """Write the evidence tree under `dest/context`; return what landed, so the mission describes only that."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    ctx = dest / "context"
    ctx.mkdir(parents=True)
    ids = list(tasks)
    if len(ids) == 1:
        (ctx / "task.md").write_text(tasks[ids[0]], encoding="utf-8")
    else:
        (ctx / "tasks").mkdir()
        index = [f"# {len(ids)} tasks in this step", "", "Each was rolled out once under the same harness; the "
                 "rollouts are `rollouts/<id>.md`, and each task's own text is `tasks/<id>.md`.", ""]
        for tid in ids:
            (ctx / "tasks" / f"{_safe(tid)}.md").write_text(tasks[tid], encoding="utf-8")
            index += [f"## {tid}", "", tasks[tid].strip(), ""]
        (ctx / "task.md").write_text("\n".join(index), encoding="utf-8")
    extras: list[tuple[str, str]] = []
    for rel, content, what in extra:
        target = ctx / S.normalize_rel(rel)
        if isinstance(content, Path):
            if content.is_dir():
                _copy_tree(content, target, surface)
                extras.append((rel.rstrip("/") + "/", what))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(content), encoding="utf-8")
            extras.append((rel, what))
    n_src = 0
    for tid, src in (source or {}).items():
        n_src += copy_source(src, ctx / "source" / _safe(tid), leak_globs)
    for label, notes in (decisions or {}).items():
        (ctx / "decisions").mkdir(exist_ok=True)
        (ctx / "decisions" / f"{label}.md").write_text(notes or "No decision notes returned.", encoding="utf-8")
    got: list[str] = []
    failed: list[str] = []
    if rollouts:
        (ctx / "rollouts").mkdir()
        (ctx / "deliverables").mkdir()
    for r in rollouts:
        if r.trajectory.strip():
            got.append(r.label)
        else:
            failed.append(f"{r.label}: {r.error or 'empty trajectory'}")
        (ctx / "rollouts" / f"{_safe(r.label)}.md").write_text(
            r.trajectory if r.trajectory.strip() else f"(trajectory unavailable: {r.error or 'empty'})\n",
            encoding="utf-8")
        (ctx / "deliverables" / f"{_safe(r.label)}.md").write_text(r.deliverable or "(no deliverable produced)\n",
                                                                    encoding="utf-8")
    n_h = 0
    if harness_dir and Path(harness_dir).is_dir():
        assert_no_leak_paths(harness_dir, leak_globs)
        n_h = _copy_tree(Path(harness_dir), ctx / "harness", surface)
    got_pool = []
    for label, cand_dir, rolls in pool:
        d = ctx / "pool" / label
        d.mkdir(parents=True, exist_ok=True)
        if cand_dir and Path(cand_dir).is_dir():
            assert_no_leak_paths(cand_dir, leak_globs)
            _copy_tree(Path(cand_dir), d / "harness", surface)
            if surface is not None:
                ref = S.read_tree(surface, ctx / "harness")
                cand = S.read_tree(surface, cand_dir)
                (d / "harness.diff").write_text(S.unified_diff(ref, cand, tolabel=label) or
                                                "(no difference from the current harness)\n", encoding="utf-8")
        if rolls:
            (d / "rollouts").mkdir(exist_ok=True)
            (d / "produced").mkdir(exist_ok=True)
            for r in rolls:
                (d / "rollouts" / f"{_safe(r.task_id)}.md").write_text(
                    r.trajectory if r.trajectory.strip() else f"(unavailable: {r.error or 'empty'})\n",
                    encoding="utf-8")
                (d / "produced" / f"{_safe(r.task_id)}.md").write_text(r.deliverable or "(nothing produced)\n",
                                                                       encoding="utf-8")
        got_pool.append(label)
    lib = False
    if library_dir and Path(library_dir).is_dir() and any(Path(library_dir).iterdir()):
        assert_no_leak_paths(library_dir, leak_globs)
        shutil.copytree(library_dir, ctx / "library", dirs_exist_ok=True)
        lib = True
    assert_no_leak_paths(ctx, leak_globs)
    assert_no_grader_verdicts(ctx)
    if rollouts and not got:
        raise RuntimeError("no trajectory could be rendered for ANY rollout -- the proposer would be adapting on "
                           "nothing: " + "; ".join(failed[:4]))
    return Evidence(root=dest, tasks=ids, rollouts=got, harness_files=n_h, library=lib, pool=got_pool,
                    decisions=len(decisions or {}), source_files=n_src, failed=failed, extras=extras)


def copy_source(src: Path | str, dst: Path, leak_globs: Sequence[str]) -> int:
    """Copy a task's source material minus answer-key paths; return the file count."""
    src = Path(src)
    if not src.is_dir():
        return 0
    n = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(src).as_posix()
        if _leaks(rel, leak_globs):
            continue
        t = dst / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
        n += 1
    return n


def _copy_tree(src: Path, dst: Path, surface: S.SurfaceSpec | None) -> int:
    """Copy a component tree (the editable files only, when a surface is given); return the file count."""
    n = 0
    for p in sorted(src.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc" or ".git" in p.parts:
            continue
        rel = p.relative_to(src).as_posix()
        if surface is not None and not S.is_allowed(surface, rel):
            continue
        t = dst / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)
        n += 1
    return n


def _glob_variants(pattern: str) -> list[str]:
    """A task glob, normalised: only a literal "./" prefix is stripped (dot-directories such as `.private/` keep
    their dot), and a leading `**/` also matches at the top level."""
    pat = str(pattern).replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    out = [pat, f"*/{pat}"]
    while pat.startswith("**/"):
        pat = pat[3:]
        out += [pat, f"*/{pat}"]
    return out


def _leaks(rel: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel, v) for pattern in patterns or () for v in _glob_variants(pattern))


def assert_no_leak_paths(root: Path | str, patterns: Sequence[str]) -> None:
    """Reject task-declared answer-key paths before a context reaches a model."""
    root = Path(root)
    if not patterns or not root.is_dir():
        return
    hits = [p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and _leaks(p.relative_to(root).as_posix(), patterns)]
    if hits:
        raise ValueError(f"answer-key path(s) would enter model context: {', '.join(sorted(hits)[:8])}")


# Upstream's markers (its benchmarks' grader output) plus the framework's own: a material record's judge block,
# the Trainer's check feedback and the OfficeQA judge's feedback wording.
GRADER_VERDICT_MARKERS: tuple[str, ...] = (
    "deterministic_eval_summary", "deterministic_eval_result", "judge_scope_and_deterministic_status",
    "Judge findings on the rendered result", '"passed_count"', '"pass_rate"', '"test_pass_rate"',
    '"judge":', '"score":', '"feedback":', "check_feedback", '"mean_score"',
    "the reference answer is",          # examples/officeqa judge feedback
    "' but expected '",                 # examples/officeqa (SkillOpt) judge feedback
)
EXTRA_MARKERS: list[str] = []           # a run's own judge wording (evolver param `verdict_markers`)


def _markers(markers: Sequence[str] | None) -> list[str]:
    return [m.lower() for m in (GRADER_VERDICT_MARKERS if markers is None else markers)] + \
        [m.lower() for m in EXTRA_MARKERS]
_MARKER_SCAN_SUFFIXES = frozenset({".md", ".txt", ".json", ".csv", ".tsv", ".yaml", ".yml", ".html", ".htm", ".log",
                                   ".py", ".js", ".sh", ".xml", ".ini", ".toml", ".patch", ".diff", ""})


def assert_no_grader_verdicts(root: Path | str, markers: Sequence[str] | None = None) -> None:
    """Refuse a context tree containing a grader's output, by file name and by content (case-insensitive). A
    tripwire on the builders' read boundary, not the defence itself: if it fires, find the read path that leaked."""
    root = Path(root)
    found = _markers(markers)
    if not found or not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        for i, m in enumerate(found):
            if m in rel.lower():
                raise ValueError(f"grader verdict would enter model context: {rel!r} matched verdict marker #{i} in "
                                 f"its NAME. The tripwire fired: this is a leak, not a false positive to widen around.")
        if p.suffix.lower() not in _MARKER_SCAN_SUFFIXES:
            continue
        body = p.read_text(encoding="utf-8", errors="replace").lower()
        for i, m in enumerate(found):
            if m in body:
                raise ValueError(f"grader verdict would enter model context: {rel!r} contains verdict marker #{i} at "
                                 f"offset {body.index(m)}. The tripwire fired: this is a leak, not a false positive to "
                                 f"widen around.")


def assert_tree_has_no_grader_verdicts(tree: Mapping[str, str], markers: Sequence[str] | None = None) -> None:
    """The same tripwire on editor-authored text (a harness tree, a lesson), before it can be rolled or stored."""
    found = _markers(markers)
    for rel in sorted(tree):
        for i, m in enumerate(found):
            if m in rel.lower():
                raise ValueError(f"harness file {rel!r} has a grader-verdict marker (#{i}) in its NAME; rename it")
        if Path(rel).suffix.lower() not in _MARKER_SCAN_SUFFIXES:
            continue
        body = str(tree[rel]).lower()
        for i, m in enumerate(found):
            if m in body:
                raise ValueError(f"harness file {rel!r} contains grader-verdict marker #{i} ({describe_marker(i)}) at "
                                 f"offset {body.index(m)}; reword it: files carrying it cannot be served as evidence "
                                 f"to later tasks")


def describe_marker(i: int) -> str:
    """A marker described without reproducing it (a message quoting it would itself trip the wire)."""
    m = _markers(None)[i] if 0 <= i < len(_markers(None)) else ""
    return "a quoted JSON key or phrase used by graders: " + " ".join(m.replace('"', "").replace("'", "").split())[:40]


def safe_text(text: str, fallback: str = "(withheld: the text quoted a grader-verdict marker)") -> str:
    """`text` unless it carries a verdict marker; used for notes and errors that flow back into evidence."""
    low = (text or "").lower()
    return fallback if any(m in low for m in _markers(None)) else (text or "")


def mission(evidence: Evidence, *, job: str) -> str:
    """The short pointer to the evidence on disk: the job, where the evidence is, how to read it."""
    return "\n".join([
        job.strip(), "", "# YOUR EVIDENCE", "",
        "It is on disk, in `context/`. Read it -- do not answer from this message alone.", "", evidence.layout, "",
        "Every trajectory EVENT is present in the files under `context/`: nothing there was clipped to fit this "
        "mission. An individual observation may contain an explicit renderer-truncation marker, and when the "
        "evidence is inlined into this message a long trajectory may be cut in the middle at an explicit "
        "`[... omitted here ...]` marker; both markers are ours, not behavior by the agent. Do not diagnose them, or "
        "events missing at them, as agent failures.", "",
        "Read `task.md` and the current harness first. Inventory every available rollout or candidate and inspect "
        "each at least once before deciding. For a candidate, compare what changed from its editing baseline, what "
        "behavior changed in its rollout, what improved, and what regressed or remained unresolved. Then deep-read "
        "the evidence relevant to any edit. Cite the specific trajectory moment behind every change; one you cannot "
        "trace to a moment is speculation."])


def _safe(task_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(task_id))
