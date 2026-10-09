"""Default world-model access tools for hosts — the system-provided set.

The MetaWorld delivery contract (frozen-setting v1.1) gives every host the
same two-channel access to the world model's memory, regardless of what
the learner builds:

  PUSH — a snapshot catalog of the whole memory: every entry's id and its
  authored `brief:` line (up to CATALOG_MAX_ENTRIES, most recently edited
  first), injected into the host's context. It carries existence and
  outline, never the knowledge itself, so its cost is bounded.

  PULL — wm_memory(id): the full entry behind a catalog line, fetched on
  demand. What to read and when is the host's own decision and its own
  token budget.

Plus the older defaults: recall a worked example by its evidence address,
read any world-model or evidence file (low-level fallback), search the
repository. The database venue has carried those three since v6 as the
RECALL/READ/SEARCH channel actions (clbench
src/systems/metaworld/system.py); the banking venue shipped without them,
so its briefings carried hundreds of evidence addresses no host could
follow (audit 2026-09-02).

Everything here is venue-agnostic: the flywheelbench Tool wrappers ride
the capability-tool slot (mwm.capabilities.as_tools shape), and the plain
functions (catalog, read_memory) serve any harness that injects text or
serves channel actions.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import clbench_recall

# Same values as the database venue's channel serving.
TOOL_OUT_CHARS = 4000
SEARCH_MAX_HITS = 20

# Memory delivery (frozen-setting v1.1).
CATALOG_MAX_ENTRIES = 100
BRIEF_MAX_CHARS = 300
# A memory entry is knowledge prose, not a log; observed sizes run 1-60K
# chars (banking kimi max 61,894). A 4,000-char cap would cripple exactly
# the entries most worth reading, so the pull channel gets its own cap.
MEMORY_OUT_CHARS = 24000


def memory_dir(wrepo: str | Path) -> Path:
    """The knowledge-entry directory: memory/ on v1.1 repos, claims/ on
    repos initialized before the rename. Old run directories stay readable
    by every analysis tool without migration."""
    w = Path(wrepo)
    m = w / "memory"
    return m if m.is_dir() else w / "claims"


def inside(root: Path, p: Path) -> bool:
    """Does `p` stay under `root` once symlinks are resolved? Every host-side
    read of the repository goes through this: a link the learner plants
    (`memory/x.md -> ../../mw_results.jsonl`) must read as absent, not as
    the host file it points at (canary measured 2026-09-13)."""
    r, q = os.path.realpath(root), os.path.realpath(p)
    return q == r or q.startswith(r + os.sep)


def _entry_files(mdir: Path) -> list[Path]:
    # the boundary is the REPOSITORY, never the memory directory itself:
    # a `memory/` that is a link to the outside would carry the boundary
    # with it (review item 21)
    root = mdir.parent
    if not mdir.is_dir() or not inside(root, mdir):
        return []
    return [p for p in mdir.iterdir()
            if p.suffix == ".md" and not p.name.startswith("_")
            and p.name != "README.md" and inside(root, p)]


def _brief_of(path: Path) -> str:
    """The entry's authored brief: line, or — for entries written before
    the field existed — the first prose line, mechanically truncated. The
    fallback keeps old repos in the catalog rather than invisible."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    in_front = True
    first_prose = ""
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("brief:"):
            return s[len("brief:"):].strip()[:BRIEF_MAX_CHARS]
        if in_front:
            if s and ":" not in s.split(" ")[0]:
                in_front = False
        if not in_front and s and not first_prose:
            first_prose = s
    return first_prose[:BRIEF_MAX_CHARS]


def _last_edit_ns(wrepo: Path, path: Path) -> int:
    """Ordering key for the catalog: the file's last git commit time,
    falling back to filesystem mtime for uncommitted edits. Editing an
    entry — its brief line included — bumps it toward the catalog's top,
    which is the learner's priority knob."""
    try:
        out = subprocess.run(
            ["git", "-C", str(wrepo), "log", "-1", "--format=%ct", "--",
             str(path.relative_to(wrepo))],
            capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            return int(out) * 1_000_000_000
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


# The database venue rebuilds the host prompt on every model turn, and the
# ordering key costs one git subprocess per entry. Entries only change when
# a learner session commits (between questions), so a fingerprint of
# (name, mtime) pairs makes every intra-question rebuild a cache hit.
_catalog_cache: dict = {}


_OPEN = re.compile(r"^\s*open:\s*(.+?)\s*$", re.M)


def open_questions(wrepo: str | Path, cap: int = 40) -> str:
    """v1.8: what is still open, compiled from `open:` lines inside memory
    entries — one line per quantity the learner marked as not yet
    settled (its current reading and the case shape that would settle
    it). Most recently edited entries first. "" when none."""
    wrepo = Path(wrepo)
    mdir = memory_dir(wrepo)
    if not mdir.is_dir():
        return ""
    files = sorted(_entry_files(mdir),
                   key=lambda p: -_last_edit_ns(wrepo, p))
    out = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for m in _OPEN.finditer(text):
            out.append(f"- [{p.stem}] {m.group(1)}")
            if len(out) >= cap:
                return "\n".join(out)
    return "\n".join(out)


def _stamp_of(path: Path) -> int | None:
    """The entry's own last_confirmed_t, or None when it carries none."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^last_confirmed_t:\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def index(wrepo: str | Path, now_t: int = 0,
          cap: int = CATALOG_MAX_ENTRIES) -> str:
    """The meta-agent's view of memory (V2.8.0): the hosts' catalog — same
    entries, same order, same brief — with each entry's own
    last_confirmed_t stamp on the line, and a marker on entries whose
    stamp is old by the staleness rule. Carried in every session prompt
    so the meta-agent can decide which entries to open without printing
    the directory (measured on banking b12: 2–4 whole-directory reads per
    session, most of them cut by the harness's output limit). "" when the
    memory is empty."""
    from . import staleness as _st
    w = Path(wrepo)
    files = _entry_files(memory_dir(w))
    if not files:
        return ""
    files.sort(key=lambda p: _last_edit_ns(w, p), reverse=True)
    lines = []
    for p in files[:cap]:
        stamp = _stamp_of(p)
        head = f"[{p.stem}] t={stamp if stamp is not None else '?'}"
        old = ""
        if now_t and stamp is not None:
            age = now_t - stamp
            if age > _st.OLD_TICKS_FRAC * max(now_t, 1):
                old = f" (unverified for {age} ticks)"
        lines.append(f"{head}{old} · {_brief_of(p) or '(no brief)'}")
    dropped = len(files) - cap
    if dropped > 0:
        lines.append(f"(+{dropped} older entries not listed — "
                     f"still readable by id, findable via search)")
    return "\n".join(lines)


def catalog(wrepo: str | Path, cap: int = CATALOG_MAX_ENTRIES) -> str:
    """The push channel: one line per memory entry, `[id] brief`, newest
    edit first, at most `cap` entries. Returns "" when the memory is
    empty so callers can skip the section entirely."""
    w = Path(wrepo)
    files = _entry_files(memory_dir(w))
    if not files:
        return ""

    def _mt(p):
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    fp = (cap, tuple(sorted((p.name, _mt(p)) for p in files)))
    hit = _catalog_cache.get(str(w))
    if hit and hit[0] == fp:
        return hit[1]
    files.sort(key=lambda p: _last_edit_ns(w, p), reverse=True)
    dropped = len(files) - cap
    lines = [f"[{p.stem}] {_brief_of(p) or '(no brief)'}"
             for p in files[:cap]]
    if dropped > 0:
        lines.append(f"(+{dropped} older entries not listed — "
                     f"still readable by id, findable via search)")
    out = "\n".join(lines)
    _catalog_cache[str(w)] = (fp, out)
    return out


def read_memory(wrepo: str | Path, name: str) -> str:
    """The pull channel: the full entry behind a catalog id."""
    name = (name or "").strip().removesuffix(".md")
    if not name or "/" in name or name.startswith("."):
        return f"not a memory id: {name!r} — use an id from the catalog"
    mdir = memory_dir(Path(wrepo))
    p = mdir / f"{name}.md"
    if not p.is_file() or not inside(Path(wrepo), p):
        near = [f.stem for f in _entry_files(mdir)
                if name.lower() in f.stem.lower()]
        hint = f" — close matches: {', '.join(near[:5])}" if near else ""
        return f"no memory entry named {name!r}{hint}"
    try:
        text = p.read_text(errors="replace")
    except OSError as exc:
        return f"channel error: {exc}"
    if len(text) > MEMORY_OUT_CHARS:
        return (text[:MEMORY_OUT_CHARS]
                + f"\n[... truncated at {MEMORY_OUT_CHARS} chars of "
                f"{len(text)}]")
    return text


def _read_target(wrepo: Path, evidence_dir: Path, target: str) -> str:
    """Port of the database venue's _read_target: /world-model/... and
    /evidence/... paths only, escape-guarded, dirs listed, output capped."""
    target = (target or "").strip()
    if target.startswith("/world-model/"):
        p = wrepo / target[len("/world-model/"):]
    elif target.startswith("/evidence/"):
        p = evidence_dir / target[len("/evidence/"):]
    else:
        return (f"not found: {target!r} — give a /world-model/... or "
                f"/evidence/... path")
    p = p.resolve()
    roots = (wrepo.resolve(), evidence_dir.resolve())
    if not any(str(p).startswith(str(r) + os.sep) or p == r for r in roots):
        return "denied: path escapes the world model"
    if p.is_dir():
        try:
            return "\n".join(sorted(x.name
                                    for x in p.iterdir()))[:TOOL_OUT_CHARS]
        except OSError as exc:
            return f"channel error: {exc}"
    try:
        return p.read_text(errors="replace")[:TOOL_OUT_CHARS]
    except OSError:
        return f"not found: {target!r}"


def _search(wrepo: Path, needle: str) -> str:
    """Port of the database venue's _search: case-insensitive substring
    over the repository, .git skipped, capped hit list."""
    if not needle:
        return "empty search"
    hits = []
    for dirpath, dirs, files in os.walk(wrepo):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fn in files:
            fp = Path(dirpath) / fn
            if not inside(wrepo, fp):
                continue
            try:
                for i, line in enumerate(
                        fp.read_text(errors="replace").splitlines(), 1):
                    if needle.lower() in line.lower():
                        rel = fp.relative_to(wrepo)
                        hits.append(f"/world-model/{rel}:{i}: "
                                    f"{line.strip()[:160]}")
                        if len(hits) >= SEARCH_MAX_HITS:
                            return "\n".join(hits)
            except OSError:
                continue
    return "\n".join(hits) if hits else "no matches"
