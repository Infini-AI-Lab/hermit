"""Async collaboration tools: a lemma library, a subgoal map and a typed channel shared by the agents that prove
one theorem at the same time. Design: docs/async-collab-adapter-design-2026-09-27.md.

State is a directory (COLLAB_DIR) shared by all agents:

  state.json          lemmas, subgoals, agents, events (a growing sequence), messages
  verified/<f>.lean   the verified source of a library lemma
  channel.jsonl       the channel, one message per line
  state.lock          taken for every read-modify-write

Every tool returns its own result plus a `team` string: what changed since the caller's last call.
`check` runs Lean (the only tool that does); the others only read and write the state.
"""
import contextlib
import fcntl
import hashlib
import uuid
import json
import os
import sys as _sys
import re
import subprocess
import time
from pathlib import Path

TYPES = ("idea", "plan", "feedback", "request", "note")
MAX_ERROR_NOTE = 1500   # characters of a failed attempt's error kept on a part
MAX_MESSAGE = 5000      # 2026-09-27: was 300; half of the messages were rejected
MAX_EVIDENCE = 5000
MAX_ERRORS_CHARS = 60000   # the full error blocks of a compile, not just their first lines (v4: was 20000, still cut)
CLEAN_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
REQUEST_MIN = 30        # an unanswered request stays in the digest this long
MAX_OUTPUT_CHARS = 20000
COMPILE_TIMEOUT = 1500      # seconds; on expiry `check` returns the diagnostics printed so far instead of nothing
DECL_RE = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*(?:theorem|lemma|def|abbrev|instance)\s+([A-Za-z_][\w'.?!]*)", re.M)
VARIABLE_RE = re.compile(r"^(variable\b|open\b(?!.*\bin\s*$)|set_option\s+(?!linter\.)(?!.*\bin\s*$)|attribute\s*\[|noncomputable section\b)")
AUX_RE = re.compile(r"^(match_\d+|proof_\d+|eq_\d+|_.*|\d+)$")
STUCK_MIN = 20          # a part open this long with an owner is offered to a second agent
HARD_MIN = 40           # a part still open this long after its first claim is "hard": parallel attempts with distinct approaches
FRESH_MIN = 20          # a part claimed less than this ago is hidden from the others
STRUCT_RE = re.compile(r"^\s*(end\b|namespace\b|section\b|noncomputable section\b|open\b(?!.*\bin\s*$)|variable\b|universe\b|set_option\b(?!.*\bin\s*$)|--|/-(?!-).*-/\s*$|$)")
MODIFIER_IN_RE = re.compile(r"^\s*(open|set_option|omit|include)\b.*\bin\s*$")
DUMP = """
open Lean Elab Command in
run_cmd do
  let env ← getEnv
  for (n, ci) in env.constants.map₂.toList do
    let kind := match ci with
      | .thmInfo _ => "thm" | .defnInfo _ => "def" | .axiomInfo _ => "axiom" | .opaqueInfo _ => "opaque"
      | .inductInfo _ => "ind" | .ctorInfo _ => "ctor" | .recInfo _ => "rec" | .quotInfo _ => "quot"
    let canon (e : Expr) : Expr := e.replace fun s => if s.isAppOf ``sorryAx then some (mkConst ``sorryAx) else none
    let ax ← collectAxioms n
    let used := ci.getUsedConstantsAsSet.toList.filter (fun m => env.constants.map₂.contains m)
    let vh : UInt64 := match ci.value? with | some v => (canon v).hash | none => 0
    logInfo m!"DECL {n} {kind} {(canon ci.type).hash} VH {vh} AX {ax} USES {used}"
"""


try:   # v12s: under the standalone service the caller's settings come from a context variable (wiring only, no rule change)
    from harnessadapter.runtime.agent_context import get as _ctx_get
except ImportError:
    _ctx_get = os.environ.get


def _env(key):
    value = _ctx_get(key)
    if not value:
        raise RuntimeError(f"{key} is not set for this agent")
    return value


def _dir() -> Path:
    d = Path(_env("COLLAB_DIR"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "verified").mkdir(exist_ok=True)
    return d


def _me() -> str:
    return _env("COLLAB_AGENT")


def file_of(name: str) -> str:
    return name.replace("'", "_p").replace(".", "_") + ".lean"


class _State:
    """Locked read-modify-write of state.json."""

    def __init__(self, d: Path):
        self.d = d
        self.path = d / "state.json"
        self.lock = open(d / "state.lock", "a+")

    def __enter__(self):
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "seq": 0, "t0": time.time(), "events": [], "lemmas": {}, "subgoals": {}, "agents": {}, "messages": []}
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            os.replace(tmp, self.path)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def event(self, **e):
        self.data["seq"] += 1
        e["seq"] = self.data["seq"]; e["t"] = time.time()
        self.data["events"].append(e)
        return e

    def agent(self, who):
        return self.data["agents"].setdefault(who, {"last_seen": 0, "working_on": None, "plan": None, "idle": 0, "checks": 0, "decls": {}, "pulled": []})


# ---------- Lean source handling ----------
@contextlib.contextmanager
def _workspace_lock(d, work):
    """v12.2: one lock per agent workspace, held only while a harness tool reads or rewrites that agent's
    Problem.lean (pull's read-insert-write, the snapshot a check compiles, the assembled proof's write). Order:
    the workspace lock before the library lock, never the other way. A compile never holds a lock. The lock file
    lives in the library directory, which the agents cannot write."""
    key = hashlib.sha1(str(Path(work).resolve()).encode()).hexdigest()[:12]
    f = open(Path(d) / f".workspace_{key}.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN); f.close()


def declarations(src: str) -> list:
    """Top-level declarations in file order: (short name, text, has_sorry). A declaration runs from its
    docstring / attribute line to the line before the next declaration."""
    lines = src.split("\n")
    starts = []
    for i, line in enumerate(lines):
        m = DECL_RE.match(line)
        if m and not line.startswith(" "):
            starts.append((i, m.group(1).split(".")[-1]))
    begins = []
    for i, name in starts:   # a declaration begins at its docstring / attribute lines
        start = i
        while start > 0 and lines[start - 1].strip().startswith("@["):
            start -= 1
        if start > 0 and lines[start - 1].rstrip().endswith("-/"):
            j = start - 1
            while j >= 0 and not lines[j].lstrip().startswith("/-"):   # the comment block right above, whatever kind
                j -= 1
            if j >= 0 and lines[j].lstrip().startswith("/--"):   # only a docstring belongs to the declaration
                start = j
        while start > 0 and MODIFIER_IN_RE.match(lines[start - 1]):   # `open Foo in` belongs to the declaration
            start -= 1
        begins.append((start, name))
    out = []
    for k, (start, name) in enumerate(begins):
        end = begins[k + 1][0] if k + 1 < len(begins) else len(lines)
        while end > start + 1 and STRUCT_RE.match(lines[end - 1]):   # `end Helpers`, `section`, blank lines are not part of it
            end -= 1
        text = "\n".join(lines[start:end]).rstrip("\n")
        if not text.strip() or not DECL_RE.search(text):
            continue   # a mis-split: not a declaration
        out.append((name, text, re.search(r"\bsorry\b", text) is not None))
    return out


def variable_context(src: str, text: str) -> list:
    """The top-level `variable` lines that precede a declaration in its file (approximation: sections are not
    tracked). Stored with the verified lemma so that a pull carries the binders the statement relies on."""
    i = src.find(text)
    if i < 0:
        return []
    out = []
    for line in src[:i].split("\n"):
        if VARIABLE_RE.match(line) and line not in out:
            out.append(line.rstrip())
    return out


def with_context(ctx: list, text: str) -> str:
    """The lemma inside a section that re-creates its file's preamble (variables, opens, options, noncomputable)."""
    if not ctx:
        return text.rstrip("\n") + "\n"
    nc = any(l.strip().startswith("noncomputable section") for l in ctx)
    body = [l for l in ctx if not l.strip().startswith("noncomputable section")]
    return ("noncomputable section\n" if nc else "section\n") + "\n".join(body) + "\n\n" + text.rstrip("\n") + "\nend\n"


def strip_comments(text: str) -> str:
    text = re.sub(r"/-.*?-/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def header(text: str) -> str:
    """The statement: from the declaration keyword to `:=` (proof excluded), whitespace collapsed."""
    body = re.sub(r"/--.*?-/\s*", "", text, flags=re.S)
    body = re.sub(r"^@\[[^\]]*\]\s*", "", body.strip())
    i = body.find(":=")
    return re.sub(r"\s+", " ", body[:i] if i > 0 else body).strip()


def parse_dump(out: str) -> dict:
    res = {}
    for n, kind, th, vh, ax, used in re.findall(r"DECL (\S+) (\S+) (\d+) VH (\d+) AX \[([^\]]*)\] USES \[([^\]]*)\]", out, re.S):
        short = n.split(".")[-1]
        if kind != "thm":
            th = f"{th}:{vh}"   # a definition is identified by its type and its body (two defs of the same type are not the same statement)
        res[short] = dict(name=n, kind=kind, typehash=th, axioms={a.strip() for a in ax.split(",") if a.strip()},
                          uses={u.strip().split(".")[-1] for u in used.split(",") if u.strip()}, aux=bool(AUX_RE.match(short)))
    return res


def uses_closure(info: dict, n: str) -> set:
    """Constants a declaration uses, seen through its auxiliary constants (match_1, proof_2, …)."""
    seen, out, queue = set(), set(), [n]
    while queue:
        m = queue.pop()
        if m in seen or m not in info:
            continue
        seen.add(m)
        for u in info[m]["uses"]:
            if u in info and info[u]["aux"]:
                queue.append(u)
            else:
                out.add(u)
    out.discard(n)
    return out


def compile_with_dump(problem: Path, work: Path, repo: str = None, lake: str = None, text: str = None) -> tuple:
    """Compile `text` (default: the file's current content) as `problem`. v12.2: every compile has its own
    temporary file, so compiles of one workspace never overwrite each other."""
    repo = repo or _env("COLLAB_REPO"); lake = lake or _env("COLLAB_LAKE")
    tag = uuid.uuid4().hex[:10]
    g = work / (f".collab_check.{tag}.lean" if problem.name == "Problem.lean" else f".collab_check_{problem.stem}.{tag}.lean")
    g.write_text((problem.read_text() if text is None else text).rstrip() + "\n" + DUMP)
    try:
        prefix = json.loads(_ctx_get("COLLAB_COMPILE_PREFIX") or "[]")   # v12s: compile in the Lean jail
        p = subprocess.run(prefix + [lake, "env", "lean", str(g)], cwd=repo, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:   # the diagnostics printed so far, plus a synthetic error naming the stall
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        rc = 124
        out += f"\n{g}:1:0: error: compile stopped after {COMPILE_TIMEOUT} s (harness limit); the declarations reported above were processed before the stop. Move the slow proof (a large decide / interval_cases) to a scratch file, verify it there, or raise its maxHeartbeats.\n"
    finally:
        try: g.unlink()
        except OSError: pass
    out = out.replace(str(g), "Problem.lean")
    return rc, out


# ---------- compiler errors ----------
DIAG_RE = re.compile(r"^\S+\.lean:\d+:\d+: (error|warning|info)")


def error_blocks(out: str) -> list[str]:
    """Every error of a Lean run with its whole message (the goal, the expected type), not just its first line;
    the list is cut to MAX_ERRORS_CHARS in total, with a note."""
    blocks, cur = [], None
    for line in out.splitlines():
        m = DIAG_RE.match(line)
        if m:
            if cur is not None: blocks.append("\n".join(cur))
            cur = [line] if m.group(1) == "error" else None
        elif cur is not None and not line.startswith("DECL "):   # the declaration dump is not part of an error
            cur.append(line)
    if cur is not None: blocks.append("\n".join(cur))
    total, kept = 0, []
    for b in blocks:
        if len(b) > MAX_ERRORS_CHARS // 3:
            b = b[:MAX_ERRORS_CHARS // 3] + f"\n… (this error is {len(b)} characters long; cut here)"
        if total + len(b) > MAX_ERRORS_CHARS and kept:
            kept.append(f"… {len(blocks) - len(kept)} more errors not shown ({MAX_ERRORS_CHARS} characters of errors in total)"); break
        kept.append(b); total += len(b)
    return kept


# ---------- the team delta ----------
def _short(s: str, n: int = 0) -> str:
    """One line; not truncated since 2026-09-27 (n is kept for callers, 0 = no limit)."""
    s = re.sub(r"\s+", " ", s).strip()
    return s if not n or len(s) <= n else s[:n - 1] + "…"


def render_delta(st: "_State", who: str, mark_seen: bool = True) -> str:
    a = st.agent(who)
    new = [e for e in st.data["events"] if e["seq"] > a["last_seen"] and e["by"] != who]
    lines = []
    verified = [e for e in new if e["kind"] == "verified" and e["by"] != who]
    for e in verified[-10:]:
        lines.append(f"new verified: {e['name']} ({e['by']}): {_short(e['statement'])}    pull with: pull {e['name']}")
    if len(verified) > 10:
        lines.append(f"… and {len(verified) - 10} more verified lemmas, see index.md")
    parts = parts_view(st, who)   # parts exist before a map too (sorry helpers of compiling files)
    if parts and (new or a["checks"] <= 1):
        lines.append("parts:\n" + "\n".join(parts[:10]))
    for e in [e for e in new if e["kind"] == "refuted"]:
        lines.append(f"refuted: {e['name']} — {e['by']}, {_short(e['evidence'])}")
    w = a.get("working_on")
    part = next((g for g in st.data["subgoals"].values() if g["status"] == "open" and g["name"] == w), None) if w else None
    if w and (part is None or who in part["on"]):   # a part counts as "on" only when claimed (map authors are not on their own parts)
        others = [b for b, x in st.data["agents"].items() if b != who and x.get("working_on") == w] if part is None else [o for o in part["on"] if o != who]
        lines.append(f"you are on: {w}" + (f" — {', '.join(others)} also on it" if others else ""))
    msgs = [e for e in new if e["kind"] == "message" and e["by"] != who]
    if msgs:
        lines.append("channel:\n" + "\n".join(f"  [{e['type']}] {e['by']}: {_short(e['text'])}" for e in msgs[-10:]))
    pending = [m for m in st.data["messages"] if m["type"] == "request" and not m.get("answered") and m["by"] != who and time.time() - m["t"] < REQUEST_MIN * 60]
    if pending and not msgs:
        lines.append("open requests:\n" + "\n".join(f"  #{m['id']} {m['by']}: {_short(m['text'])}" for m in pending[-10:]))
    nudge = phase(st) == 1 and (a.get("plan") or a.get("route")) and not a.get("has_sorry_helpers") and who not in st.data.get("maps", {})
    if not new and not (parts and a["checks"] <= 1) and not nudge:
        return ""
    n_open = sum(1 for s in st.data["subgoals"].values() if s["status"] == "open")
    lines.append(f"full state: {len(st.data['lemmas'])} verified, {n_open} open, "
                 f"{sum(1 for s in st.data['subgoals'].values() if s['status'] == 'refuted')} refuted, "
                 f"{len(st.data['messages'])} messages — index.md in the library directory")
    if mark_seen:
        a["last_seen"] = st.data["seq"]
    ph = phase_lines(st, who)
    head = ph if phase(st) == 1 else ph[:len(ph) - len(parts_view(st, who))]   # the header lines only; the parts list is in `lines`
    return "[team since your last call]\n" + "\n".join(head + lines)


def write_updates(st: "_State"):
    """library/updates.md: the newest changes first (what every tool reply also carries), then what is open
    and who is on what. Agents read it with cat at the start of each work cycle."""
    d = st.d; t0 = st.data.setdefault("t0", time.time())
    def when(t): return f"{int((t - t0) // 60):3d} min"
    L = ["# Team updates", "", "## Phase", ""] + phase_lines(st, "") + ["", "## Newest first", ""]
    for e in reversed(st.data["events"][-200:]):
        k = e["kind"]
        if k == "verified":
            L.append(f"- {when(e['t'])}  verified: {e['name']} ({e['by']}): {_short(e['statement'])}  → pull {e['name']}")
        elif k == "open":
            L.append(f"- {when(e['t'])}  new subgoal ({e['by']}): {e['name']}: {_short(e['statement'])}")
        elif k == "refuted":
            L.append(f"- {when(e['t'])}  refuted: {e['name']} — {e['by']}: {_short(e['evidence'])}")
        elif k == "message":
            L.append(f"- {when(e['t'])}  [{e['type']}] {e['by']}: {_short(e['text'])}" + (f" (re {e['ref']})" if e.get('ref') else ""))
        elif k == "pull":
            L.append(f"- {when(e['t'])}  {e['by']} pulled {e['name']}")
        elif k == "closed":
            L.append(f"- {when(e['t'])}  part proved: {e['name']} ({e['by']})")
        elif k == "updated":
            L.append(f"- {when(e['t'])}  updated: {e['name']} ({e['by']}): {_short(e['statement'])}")
        elif k == "split":
            L.append(f"- {when(e['t'])}  {e['by']} split {e['name']} into sub-parts: {', '.join(e.get('parts', []))}")
        elif k == "dropped":
            L.append(f"- {when(e['t'])}  part dropped (no file states it any more): {e['name']}")
    if len(L) == 2:
        L.append("- nothing yet")
    L += ["", "## Open subgoals nobody is on", ""]
    free = [g for g in st.data["subgoals"].values() if g["status"] == "open" and not g["on"]]
    L += [f"- {g['name']} (from {', '.join(g['files'])}): {_short(g['statement'])}" for g in free] or ["- none"]
    L += ["", "## Who is on what", ""]
    L += [f"- {who}: {a.get('working_on') or '?'}" + (f" — plan: {a['plan']}" if a.get('plan') else "") for who, a in st.data["agents"].items()] or ["- nobody yet"]
    L += ["", "## Open requests", ""]
    req = [m for m in st.data["messages"] if m["type"] == "request" and not m.get("answered")]
    L += [f"- #{m['id']} {m['by']}: {m['text']}" for m in req] or ["- none"]
    (d / "updates.md").write_text("\n".join(L) + "\n")


_DECL_KW = re.compile(r"(?m)^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*(theorem|lemma|def|abbrev|instance|structure|inductive|class)\b")
LONG_DEF = 400   # characters; a longer definition (a data table, a structure with proofs) shows its start only


def write_lemmas(st: "_State"):
    """v12.5: library/lemmas.md, everything the team has verified, in verification order (definitions come before what
    uses them), one line per entry: a lemma's proved statement, or a definition's full text; then who, and the file.
    Entries are only appended, so numbers never change and the file can be read in pieces."""
    d = st.d
    rows = [(k, e) for k, e in st.data["lemmas"].items() if not e.get("dup_of")]
    L = ["# Library", "",
         f"{len(rows)} entries, in the order they were verified (definitions come before what uses them); new entries are only appended.",
         "Lemma: its proved statement. Definition: its full text (a very long one: its start; see the file). Then: who verified it — the file with the full source (`pull <name>` copies it into your Problem.lean).", ""]
    for i, (k, e) in enumerate(rows, 1):
        try:
            src = (d / "verified" / file_of(k)).read_text()
        except OSError:
            src = e.get("text") or ""
        m = _DECL_KW.search(src)
        if m and m.group(1) not in ("theorem", "lemma"):
            body = re.sub(r"\s+", " ", re.sub(r"\n\s*end\s*$", "", src[m.start():].strip()))
            body = body if len(body) <= LONG_DEF else body[:200] + " … (long; see the file)"
        else:
            body = re.sub(r"\s+", " ", e["statement"])
        L.append(f"{i}. {body}  — {e['by']} — library/verified/{file_of(k)}")
    (d / "lemmas.md").write_text("\n".join(L) + "\n")


def write_index(st: "_State"):
    d = st.d
    write_lemmas(st)
    L = ["# Team library", "", "## Phase", ""] + phase_lines(st, "") + ["", "## Verified lemmas (pull <name> to use one)", ""]
    for name, e in st.data["lemmas"].items():
        if e.get("dup_of"):
            continue
        L.append(f"- {name} ({e['by']}): {e['statement']}" + (f"  [uses {', '.join(e['deps'])}]" if e["deps"] else ""))
    L += ["", "## Open subgoals (sorry helpers in compiling files)", ""]
    for s in st.data["subgoals"].values():
        if s["status"] == "open":
            L.append(f"- {s['name']} ({', '.join(s['files'])}; on it: {', '.join(s['on']) or 'nobody'}"
                     + (", disputed" if s.get("disputed") else "") + f"): {s['statement']}")
    L += ["", "## Refuted", ""]
    for s in st.data["subgoals"].values():
        if s["status"] == "refuted":
            L.append(f"- {s['name']}: {s.get('evidence', '')} ({s.get('refuted_by', '')})")
    L += ["", "## Who is on what", ""]
    for who, a in st.data["agents"].items():
        L.append(f"- {who}: {a.get('working_on') or '?'}" + (f" — plan: {a['plan']}" if a.get("plan") else ""))
    L += ["", "## Channel (last 20)", ""]
    for m in st.data["messages"][-20:]:
        L.append(f"- [{m['type']}] {m['by']}: {m['text']}" + (f" (re {m['ref']})" if m.get("ref") else ""))
    (d / "index.md").write_text("\n".join(L) + "\n")


# ---------- phases ----------
def phase(st: "_State") -> int:
    """1 while nobody has a map (a target proof that compiles modulo sorry helpers), 2 afterwards."""
    return 2 if st.data.get("maps") else 1


def _age(t: float) -> str:
    return f"{int((time.time() - t) // 60)} min"


def is_hard(g: dict) -> bool:
    """A part is hard when it has been worked on for HARD_MIN minutes since its first claim and is still open."""
    cl = g.get("claimed", {})
    return g["status"] == "open" and bool(cl) and time.time() - min(cl.values()) >= HARD_MIN * 60


def approaches_text(g: dict) -> str:
    ap = g.get("approaches", {})
    return "; ".join(f"{who}: {_short(txt)}" for who, txt in ap.items())


def _child_lines(st: "_State", g: dict, who: str) -> list:
    out = []
    for kk in g.get("children", []):
        c = st.data["subgoals"].get(kk)
        if not c:
            continue
        mark = "proved" if c["status"] == "closed" else ("open, " + (", ".join(c["on"]) + " on it" if c["on"] else "nobody on it"))
        out.append(f"    ↳ {c['name']} ({mark}): {_short(c['statement'])}")
    return out


def parts_view(st: "_State", who: str) -> list:
    """The open parts as `who` should see them: free ones first, then stuck ones (a second approach is welcome),
    then the ones freshly taken by others (named only). Each line carries the difficulty signals."""
    free, stuck, taken, mine = [], [], [], []
    for g in st.data["subgoals"].values():
        if g["status"] != "open" or g.get("parent") in st.data["subgoals"] and st.data["subgoals"][g["parent"]]["status"] == "open":
            continue   # a child is listed under its parent
        owners = [o for o in g["on"] if o != who]
        tried = list(dict.fromkeys(g.get("tried", []) + [f for f in g.get("files", [])]))
        sig = f"open {_age(g['t_open'])}, {g.get('checks', 0)} checks" + (f", stated/tried by {', '.join(tried)}" if tried else "")
        if g.get("children"):
            kids = [st.data["subgoals"].get(k) for k in g["children"]]
            n_open = sum(1 for c in kids if c and c["status"] == "open")
            sig += f"; split by {g.get('split_by')} into {len(kids)} sub-parts, {n_open} open"
        same_name = st.data["lemmas"].get(g["name"])
        if same_name and _short(same_name["statement"]) != _short(g["statement"]):
            sig += f"; a verified lemma named {g['name']} exists (by {same_name['by']}) but its statement differs — pull it if it fits"

        if who in g["on"]:
            since_me = g.get("claimed", {}).get(who, g["t_open"])
            ask = ("; you have been on it for " + _age(since_me) + " — split it: state its steps as helper lemmas with sorry (they become sub-parts others can take), or say what blocks you"
                   if time.time() - since_me >= STUCK_MIN * 60 and not any(g["name"] == st.data["subgoals"].get(k, {}).get("name") for k in []) else "")
            mine.append(f"- {g['name']} (you are on it; {sig}{ask}): {_short(g['statement'])}")
        elif not owners:
            free.append(f"- {g['name']} (nobody on it; {sig}): {_short(g['statement'])}")
        else:
            durs = ", ".join(f"{o} {_age(g.get('claimed', {}).get(o, g['t_open']))}" for o in owners)
            longest = max(time.time() - g.get("claimed", {}).get(o, g["t_open"]) for o in owners)
            if is_hard(g):
                ap = approaches_text(g)
                stuck.append(f"- {g['name']} (HARD: {durs} on it; {sig}; attack it in parallel with an approach different from those declared"
                             + (f" — approaches so far: {ap}" if ap else " — no approach declared yet") + f"; say plan with ref to declare yours): {_short(g['statement'])}")
            elif len(owners) >= 2:
                taken.append(f"- {g['name']} ({durs} on it — enough people on this part; ENOUGH)")
            elif longest >= STUCK_MIN * 60 and not any(e["by"] in owners and time.time() - e["t"] < 600 for e in st.data["lemmas"].values()):
                ask = ("take one of its open sub-parts" if any(st.data["subgoals"].get(k, {}).get("status") == "open" for k in g.get("children", []))
                       else "split it — state its steps as helper lemmas with sorry in your file, they become sub-parts the team can take — or join with a different approach")
                stuck.append(f"- {g['name']} (stuck: {durs} on it; {sig}; {ask}): {_short(g['statement'])}")
            else:
                taken.append(f"- {g['name']} (taken by {durs} ago)")
    hint = "take another" if free else "join only with an approach different from theirs, or split it into smaller sorry helpers"
    out = []
    by_name = {g["name"]: g for g in st.data["subgoals"].values() if g["status"] == "open"}
    for l in free + stuck + [l.replace("ENOUGH", hint) for l in taken] + mine:
        out.append(l)
        name = l[2:].split(" (", 1)[0]
        g = by_name.get(name, {})
        out += _child_lines(st, g, who)
        att = attempts_text(g)
        if att:
            out.append("    " + att.replace("\n", "\n    "))
    return out


def attempts_text(g: dict) -> str:
    """What is known about the attempts on a part: notes left by agents, lemmas they proved while on it, the
    goal their last failing check stopped at."""
    L = []
    for who in sorted(set(list(g.get("notes", {})) + list(g.get("progress", {})) + list(g.get("last_error", {})))):
        bits = []
        for n in g.get("notes", {}).get(who, [])[-2:]:
            bits.append(f"note: {_short(n['text'])}")
        pr = g.get("progress", {}).get(who, [])
        if pr:
            bits.append("verified while on it: " + ", ".join(pr[-15:]))
        le = g.get("last_error", {}).get(who)
        if le:
            bits.append(f"last failing check ({_age(le['t'])} ago): " + _short(le["text"])[:400])
        if bits:
            L.append(f"attempt by {who}: " + " | ".join(bits))
    return "\n".join(L)


def phase_lines(st: "_State", who: str) -> list:
    """The phase header of every tool reply and of the resume message."""
    maps = st.data.get("maps", {})
    if not maps:
        routes = [b for b, x in st.data["agents"].items() if x.get("plan") or x.get("route")]
        L = ["phase 1 (search): no map yet. A map is a Problem.lean whose target proof compiles with the hard steps "
             "stated as helper lemmas with sorry; the first verified map opens phase 2, where the team takes its parts."]
        mine = (st.data["agents"].get(who, {}).get("plan") or st.data["agents"].get(who, {}).get("route")) if who else True
        L.append("routes declared by: " + (", ".join(routes) + " (their messages are in the channel and in index.md)" if routes else "nobody yet")
                 + ("" if mine else " — you have not declared yours (say plan)"))
        a = st.data["agents"].get(who, {}) if who else {}
        if mine and who and not a.get("has_sorry_helpers") and not st.data.get("maps", {}).get(who) and not any(e["by"] == who for e in st.data["lemmas"].values()):
            L.append("your route has no statement in Lean yet: write its steps as helper lemmas with sorry in Problem.lean and run check — "
                     "statements are cheap, and once they compile the team can take, refute or split them")
        return L
    n_open = sum(1 for g in st.data["subgoals"].values() if g["status"] == "open")
    open_names = {g["name"] for g in st.data["subgoals"].values() if g["status"] == "open"}
    firsts, copies = {}, {}
    for b, m in sorted(maps.items(), key=lambda kv: kv[1]["t"]):
        sig_m = tuple(sorted(m["parts"]))
        if sig_m in firsts: copies.setdefault(firsts[sig_m], []).append(b)
        else: firsts[sig_m] = b
    L = ["phase 2 (parts): map" + ("s" if len(firsts) > 1 else "") + " by " + ", ".join(f"{b} ({len(maps[b]['parts'])} parts, {sum(1 for x in maps[b]['parts'] if x in open_names)} open, {_age(maps[b]['t'])}" + (f"; copied by {', '.join(copies[b])}" if b in copies else "") + ")" for b in firsts.values())
         + f"; {n_open} parts open, {len(st.data['lemmas'])} verified. Take a part nobody is on, or a stuck one with a different approach; say plan first."]
    pv = parts_view(st, who)
    L.append("plan (the shared DAG; edit it with the plan tool):\n" + plan_tree(st))
    hard = [g["name"] for g in st.data["subgoals"].values() if is_hard(g)]
    if hard:
        L.append("hard part" + ("s" if len(hard) > 1 else "") + ": " + ", ".join(hard) + " — open for a long time; the team attacks "
                 + ("them" if len(hard) > 1 else "it") + " in parallel with distinct approaches (declare yours with say plan, ref = the part; "
                 "the attempts recorded under the part are the starting point) or splits it into sub-parts")
    free = [l for l in pv if "(nobody on it" in l]
    if n_open and not free and not any("(you are on it" in l for l in pv):
        L.append("no part is free: split a stuck part into smaller helper lemmas stated with sorry in your file (they become "
                 "parts), or take a stuck one with an approach different from the one tried")
    return L + pv


# ---------- tools ----------
def check(file: str = "Problem.lean") -> dict:
    """Compile a Lean file of my workspace (default Problem.lean). Problem.lean feeds the library: verified
    helpers go in, sorry helpers become open subgoals. Any other file is just compiled, with the team delta."""
    work = Path(_env("COLLAB_WORKSPACE"))
    if file in ("", "Problem.lean"):
        return run_check(_dir(), _me(), work, _env("COLLAB_TARGET"), _env("COLLAB_REPO"), _env("COLLAB_LAKE"), deliver=True)
    path = (work / file).resolve()
    if not path.is_relative_to(work.resolve()) or not path.is_file() or path.suffix != ".lean":
        raise ValueError("file must be a .lean file inside your workspace")
    snap = path.read_text()   # v12.2: the text compiled is the text ingested
    rc, out = compile_with_dump(path, work, text=snap)   # v4: a scratch file feeds the library too; v8: partial diagnostics on a timeout (its verified helpers), but no parts and no map
    out = out.replace("Problem.lean", file)
    errors = error_blocks(out)
    newly, dups = [], []
    try:   # v10: proving a part in a scratch file counts as being on it
        src0 = snap; me0 = _me()
        with _State(_dir()) as st:
            for n, t, s_ in declarations(src0):
                if s_:
                    continue
                for sg in st.data["subgoals"].values():
                    if sg["status"] == "open" and (sg["name"] == n or sg["statement"] == header(t)) and me0 not in sg["on"] \
                            and not any(e["statement"] == sg["statement"] for e in st.data["lemmas"].values()):
                        sg["on"].append(me0); sg.setdefault("claimed", {}).setdefault(me0, time.time())
                        st.event(kind="working", by=me0, name=sg["name"])
    except Exception:
        pass
    if not errors:
        info = parse_dump(out); src = snap
        orig_names = {n for n, _, _ in declarations((work / "Original.lean").read_text())} if (work / "Original.lean").exists() else set()
        with _State(_dir()) as st:
            newly, dups = ingest_verified(st, _me(), [(n, t, s_) for n, t, s_ in declarations(src) if n not in orig_names and n != _env("COLLAB_TARGET").split(".")[-1]], info, src)
            team = render_delta(st, _me()); write_index(st); write_updates(st)
    else:
        with _State(_dir()) as st:
            team = render_delta(st, _me())
    if newly:   # lemmas verified in a scratch file while on a part count as progress on that part
        try:
            with _State(_dir()) as st:
                for sg in st.data["subgoals"].values():
                    if sg["status"] == "open" and _me() in sg["on"]:
                        pr = sg.setdefault("progress", {}).setdefault(_me(), [])
                        for x in newly:
                            if x not in pr: pr.append(x)
        except Exception:
            pass
    res = {"file": file, "compiled": rc == 0 and not errors, "errors": errors,
           "output": "\n".join(l for l in out.splitlines() if "DECL " not in l)[-MAX_OUTPUT_CHARS:],   # v12.9: #check / exact? output also when an error is present
           "verified_now": newly, "team": team}
    if dups: res["duplicates"] = dups
    return res


def ingest_verified(st: "_State", me: str, helpers: list, info: dict, src: str = "") -> tuple:
    """Helpers of `me` that compiled without sorry and with clean axioms enter the library. Returns (new names,
    duplicate notices). A pulled lemma is never re-registered; a statement already in the library under another
    name is reported as a duplicate instead of being stored under a suffixed name."""
    d = st.d; newly, dups = [], []
    pulled = set(st.agent(me).get("pulled", []))
    by_hash = {e["typehash"]: k for k, e in st.data["lemmas"].items()}
    hnames = {h for h, _, _ in helpers}
    for n, t, s in helpers:
        ci = info.get(n)
        if s or ci is None or ci["kind"] == "axiom" or n in pulled or n.split("__")[0] in pulled:
            continue
        if not ci["axioms"] <= CLEAN_AXIOMS:   # sorryAx, native_decide, user axioms: not shareable
            continue
        ex0 = st.data["lemmas"].get(n)
        if ex0 and (ex0["typehash"] == ci["typehash"] or ex0.get("text") == _short(t) or (ci["kind"] == "thm" and ex0["statement"] == header(t))):
            continue   # the same lemma (a definition counts as the same only with the same text)
        if any(e.get("text") == _short(t) for e in st.data["lemmas"].values()):
            continue   # an identical copy is already in the library under another name
        flat = _short(t)
        other = by_hash.get(ci["typehash"]) or next((k for k, e in st.data["lemmas"].items() if (e["statement"] == header(t) if ci["kind"] == "thm" else e.get("text") == flat) and k.split("__")[0] != n), None)
        dup_of = None
        if other and other != n and other.split("__")[0] != n:
            dups.append(f"{n} states the same as {other} (by {st.data['lemmas'][other]['by']}); pull {other} instead of proving it")
            st.data.setdefault("aliases", {})[n] = other
            dup_of = other   # stored anyway under its own name: lemmas that refer to it must still pull
        code = strip_comments(t)
        deps = sorted((uses_closure(info, n) | {h for h in hnames if re.search(r"(?<![\w'])" + re.escape(h) + r"(?![\w'])", code)}) & hnames - {n})   # by the names the code uses (duplicates are stored under their own names)
        deps = [f"{d}__{me}" if (d in st.data["lemmas"] and st.data["lemmas"][d]["by"] != me and f"{d}__{me}" in st.data["lemmas"]) else d for d in deps]   # my own variant of a name, not another agent's
        ctx = variable_context(src, t) if src else []
        ex = st.data["lemmas"].get(n)
        if ex and ex["by"] == me:   # my own lemma with a changed statement: updated in place, no suffix
            key = n
            (d / "verified" / file_of(key)).write_text(with_context(ctx, t))
            by_hash.pop(ex["typehash"], None)
            ex.update(statement=header(t), typehash=ci["typehash"], deps=deps, t=time.time(), context=ctx)
            st.event(kind="updated", by=me, name=key, statement=header(t))
            by_hash[ci["typehash"]] = key
            continue
        key = n if not ex else f"{n}__{me}"
        if key in st.data["lemmas"]:
            continue
        (d / "verified" / file_of(key)).write_text(with_context(ctx, t))
        st.data["lemmas"][key] = dict(name=key, by=me, statement=header(t), typehash=ci["typehash"], deps=deps, t=time.time(), context=ctx, text=_short(t), dup_of=dup_of)
        if dup_of is None:
            st.event(kind="verified", by=me, name=key, statement=header(t))
            by_hash[ci["typehash"]] = key
            newly.append(key)
    for k in list(st.data["lemmas"]):   # no dangling dependency: a textual match that never entered the library is dropped
        st.data["lemmas"][k]["deps"] = [x for x in st.data["lemmas"][k]["deps"] if x in st.data["lemmas"]]
    return newly, dups


def run_check(d: Path, me: str, work: Path, target: str, repo: str, lake: str, deliver: bool) -> dict:
    """The body of `check`. The launcher also calls it on the host for every changed file (deliver=False): the
    library is filled whether or not the agent uses the tool; only a tool call marks the delta as seen."""
    d = Path(d); (d / "verified").mkdir(parents=True, exist_ok=True); work = Path(work); problem = work / "Problem.lean"
    target = target.split(".")[-1]   # declarations are keyed by their short name (the launcher passes the dotted name)
    with _workspace_lock(d, work):   # v12.2: a consistent snapshot; the compile runs outside the lock
        src = problem.read_text()
    orig_names = {n for n, _, _ in declarations((work / "Original.lean").read_text())} if (work / "Original.lean").exists() else set()
    t_c = time.time()
    rc, out = compile_with_dump(problem, work, repo, lake, text=src)
    compile_s = round(time.time() - t_c)
    errors = error_blocks(out)
    info = parse_dump(out) if not errors else {}
    decls = declarations(src)
    helpers = [(n, t, s) for n, t, s in decls if n not in orig_names and n != target]
    tgt = info.get(target)
    result = {"compiled": not errors, "compile_s": compile_s, "errors": errors, "target_axioms": sorted(tgt["axioms"]) if tgt else None}
    if rc == 124:
        result["timed_out"] = True
    if not errors:
        infos = [l for l in out.splitlines() if "DECL " not in l and l.strip()]
        if infos:
            result["output"] = "\n".join(infos)[-MAX_OUTPUT_CHARS:]
    with _State(d) as st:
        a = st.agent(me); a["checks"] += 1
        # working on: the last changed/new declaration that still has a sorry, else the erroring one
        prev = a.get("decls", {}); cur = {n: hashlib.sha1(t.encode()).hexdigest() for n, t, _ in decls}
        changed = [n for n, t, s in decls if cur[n] != prev.get(n)]
        mine = ({h for h, _, _ in helpers} | {target}) - set(a.get("pulled", []))   # helpers of my own and the target count as "working on", pulled names do not
        cand = [n for n, t, s in decls if n in changed and n in mine and s and prev]   # a declaration that still has a sorry (not on the very first check: everything is "changed" then)
        working = cand[-1] if cand else None
        if working is None and errors:
            m = re.search(r"Problem\.lean:(\d+):", errors[0])
            if m:
                line = int(m.group(1)); pos = 0
                for n, t, s in decls:
                    i = src.find(t, pos)
                    if i >= 0 and src.count("\n", 0, i) + 1 <= line <= src.count("\n", 0, i + len(t)) + 1:
                        working = n if n in mine else None; break
                    pos = max(pos, i)
        if working:
            if working != a.get("working_on"):
                st.event(kind="working", by=me, name=working)
            a["working_on"] = working; a["idle"] = 0
        else:
            a["idle"] += 1
            w = a.get("working_on")
            done = w and (w not in cur or (not errors and info.get(w) and "sorryAx" not in info[w]["axioms"]))
            if a["idle"] >= 4 or done:   # the declaration is proved or gone: no longer "on" it
                a["working_on"] = None
        a["decls"] = cur
        a["has_sorry_helpers"] = any(s_ for _, _, s_ in helpers)
        # verified helpers -> library
        newly, dups = [], []
        if not errors:
            newly, dups = ingest_verified(st, me, helpers, info, src)
        # a part I am proving (inferred claim): same statement as an open part, sorry gone, not verified yet —
        # with or without compile errors
        claimed_keys = set()
        for n, t, s_ in helpers:
            if s_:
                continue
            ci = info.get(n)
            hh = hashlib.sha1(header(t).encode()).hexdigest()[:12]
            for key, sg in st.data["subgoals"].items():
                if sg["status"] != "open" or not (key == (ci["typehash"] if ci else None) or key == hh or sg["statement"] == header(t)):
                    continue
                if ci is not None and not errors and "sorryAx" not in ci["axioms"]:
                    continue   # proved here: closed below
                if me not in sg["on"]:
                    sg["on"].append(me); sg.setdefault("claimed", {})[me] = time.time()
                    st.event(kind="working", by=me, name=sg["name"])
                claimed_keys.add(key)
                if working is None:
                    working = sg["name"]; a["working_on"] = working; a["idle"] = 0
        if not errors:
            # open subgoals: sorry helpers, merged across agents by type hash (statement text when unknown)
            seen_keys = set(claimed_keys)
            for n, t, s in helpers:
                if not s:
                    continue
                ci = info.get(n)
                key = ci["typehash"] if ci else hashlib.sha1(header(t).encode()).hexdigest()[:12]
                seen_keys.add(key)
                sg = st.data["subgoals"].get(key)
                if sg is not None and sg["status"] == "dropped":
                    sg["status"] = "open"; sg["t_open"] = time.time(); st.event(kind="open", by=me, name=sg["name"], statement=sg["statement"])
                if sg is None:   # a proposed plan node of the same name is bound to this statement
                    pk, pg = _find_node(st, n)
                    if pg is not None and pg["status"] == "open" and pk.startswith("plan:"):
                        st.data["subgoals"][key] = pg; del st.data["subgoals"][pk]
                        for og in st.data["subgoals"].values():
                            if og.get("parent") == pk: og["parent"] = key
                            if pk in og.get("children", []): og["children"] = [key if c == pk else c for c in og["children"]]
                        for m in st.data.get("maps", {}).values():
                            pass
                        sg = pg; sg["statement"] = header(t); sg["lean"] = True
                    else:
                        sg = st.data["subgoals"][key] = dict(name=n, statement=header(t), status="open", files=[], on=[], t_open=time.time())
                        st.event(kind="open", by=me, name=n, statement=header(t))
                if me not in sg["files"]:
                    sg["files"].append(me)
                    if not sg.get("parent"):   # a new statement from an agent who is on a part: a sub-part of it (they split flat)
                        mine_on = [k2 for k2, g2 in st.data["subgoals"].items() if g2["status"] == "open" and me in g2["on"] and k2 != key]
                        if len(mine_on) == 1:
                            sg["parent"] = mine_on[0]; st.data["subgoals"][mine_on[0]].setdefault("children", [])
                            if key not in st.data["subgoals"][mine_on[0]]["children"]:
                                st.data["subgoals"][mine_on[0]]["children"].append(key)
                                st.event(kind="split", by=me, name=st.data["subgoals"][mine_on[0]]["name"], parts=[n])
            # edges from the compiled uses relation: S (sorry helper) used by D (a node of the plan) -> S under D
            for n, t, s_ in helpers:
                if s_ or n not in info:
                    continue
                dk, dg = _find_node(st, n)
                if dg is None:
                    continue
                for u in uses_closure(info, n):
                    uk, ug = _find_node(st, u)
                    if ug is not None and ug["status"] == "open" and uk != dk and not ug.get("parent"):
                        ug["parent"] = dk; dg.setdefault("children", [])
                        if uk not in dg["children"]: dg["children"].append(uk)
            # a split: a helper of mine stating an open part, sorry removed, compiling modulo sorry sub-helpers
            sorry_names = {h for h, _, s_ in helpers if s_}
            key_of = {}
            for n2, t2, s2 in helpers:
                if s2:
                    ci2 = info.get(n2)
                    key_of[n2] = ci2["typehash"] if ci2 else hashlib.sha1(header(t2).encode()).hexdigest()[:12]
            for n, t, s_ in helpers:
                if s_:
                    continue
                ci = info.get(n)
                if not ci or "sorryAx" not in ci["axioms"]:
                    continue
                for key, sg in st.data["subgoals"].items():
                    if sg["status"] != "open" or not (key == ci["typehash"] or sg["statement"] == header(t)):
                        continue
                    kids = sorted(u for u in uses_closure(info, n) if u in sorry_names)
                    kid_keys = [key_of[k] for k in kids if k in key_of]
                    if kid_keys and set(kid_keys) != set(sg.get("children", [])):
                        sg["children"] = kid_keys; sg["split_by"] = me
                        for kk in kid_keys:
                            if kk in st.data["subgoals"]:
                                st.data["subgoals"][kk]["parent"] = key
                        st.event(kind="split", by=me, name=sg["name"], parts=kids)
            # a part proved by anyone (same statement now in the library) is closed
            for key, sg in st.data["subgoals"].items():
                if sg["status"] != "open":
                    continue
                prover = next((e["by"] for e in st.data["lemmas"].values() if e["typehash"] == key or _short(e["statement"]) == _short(sg["statement"]) or (key.startswith("plan:") and e["name"].split("__")[0] == sg["name"])), None)
                if prover:
                    sg["status"] = "closed"; sg["proved_by"] = prover; st.event(kind="closed", by=prover, name=sg["name"])
            for key, sg in st.data["subgoals"].items():
                if sg["status"] == "open" and me in sg["files"] and key not in seen_keys:
                    sg["files"].remove(me)
                    if me in sg["on"]:
                        sg["on"].remove(me)
                    if not sg["files"] and not sg["on"]:
                        sg["status"] = "dropped"; st.event(kind="dropped", by=me, name=sg["name"])
            # the map: a target proof that compiles modulo sorry helpers
            ti = info.get(target); ttext = next((t for n, t, s in decls if n == target), "")
            tsorry = re.search(r"\bsorry\b", ttext) is not None
            orig_sorry = {n for n, t, s_ in decls if s_ and n in orig_names and n != target}
            bad = sorted(u for u in uses_closure(info, target) if u in orig_sorry) if ti else []
            if bad:
                result["advice"] = (f"the target's proof depends on unproved theorem(s) of the original file ({', '.join(bad)}): that does not "
                                    "count as a map or a proof; state what you need as your own helper lemma and prove it")
            if ti and "sorryAx" in ti["axioms"] and not tsorry and not bad:
                sorry_helpers = {h for h, _, s in helpers if s}; hnames = {h for h, _, _ in helpers}
                seen_m, queue_m, parts = set(), [target], set()
                while queue_m:   # through every helper of the file, so a part two steps below the target counts
                    x = queue_m.pop()
                    for u in uses_closure(info, x):
                        if u in sorry_helpers:
                            parts.add(u)
                        if u in hnames and u not in seen_m:
                            seen_m.add(u); queue_m.append(u)
                parts = sorted(parts)
                if parts:
                    maps = st.data.setdefault("maps", {})
                    if me not in maps:
                        st.event(kind="map", by=me, name=target, parts=parts)
                    maps[me] = dict(parts=sorted(set(maps.get(me, {}).get("parts", [])) | set(parts)), t=maps.get(me, {}).get("t", time.time()))
                    maps[me].update(glue=ttext, ctx=variable_context(src, ttext), parts_now=parts, t_glue=time.time())   # v12.1: what the automatic assembly needs
        # leaving a part: I am on it but my file no longer states it -> released, and a note is asked for
        stated = {_short(header(t)) for _, t, _ in decls} | {n for n, _, _ in decls}
        left = []
        for key, sg in st.data["subgoals"].items():
            if sg["status"] == "open" and me in sg["on"] and _short(sg["statement"]) not in stated and sg["name"] not in stated:
                sg["on"].remove(me); left.append(sg["name"])
                if not sg.get("notes", {}).get(me):
                    st.event(kind="left", by=me, name=sg["name"])
        if left:
            result["left_parts"] = [f"you no longer state {n} in Problem.lean: you are off it; leave a note (`say note ref={n}`: the approach you tried, what you proved, the goal you could not close) so the next agent starts from it" for n in left]
        # difficulty signals on the parts the caller is on (failing checks count too), and the attempt record:
        # the error of the part's own declaration when the check failed, the lemmas verified while on it
        for key, sg in st.data["subgoals"].items():
            if sg["status"] == "open" and me in sg["on"]:
                sg["checks"] = sg.get("checks", 0) + 1
                sg.setdefault("claimed", {}).setdefault(me, time.time())
                if me not in sg.setdefault("tried", []):
                    sg["tried"].append(me)
                if newly:
                    pr = sg.setdefault("progress", {}).setdefault(me, [])
                    for x in newly:
                        if x not in pr: pr.append(x)
                if errors:
                    own = next((t for n, t, s_ in decls if n == sg["name"]), None)
                    err = None
                    if own:
                        j = src.find(own)
                        if j >= 0:
                            lo = src.count("\n", 0, j) + 1; hi = src.count("\n", 0, j + len(own)) + 1
                            err = next((e for e in errors if (m := re.match(r"Problem\.lean:(\d+):", e)) and lo <= int(m.group(1)) <= hi), None)
                    if err:
                        sg.setdefault("last_error", {})[me] = dict(t=time.time(), text=err[:MAX_ERROR_NOTE])
        result["verified_now"] = newly
        if dups: result["duplicates"] = dups
        result["verified"] = sorted(n for n, _, s in helpers if not s and (n in st.data["lemmas"] or f"{n}__{me}" in st.data["lemmas"]))
        result["phase"] = phase(st)
        if tgt and not errors and not (tgt["axioms"] <= CLEAN_AXIOMS) and "sorryAx" not in tgt["axioms"]:
            result["advice"] = ("the target's proof depends on axioms outside propext / Classical.choice / Quot.sound "
                                "(native_decide or a custom axiom): it does not count as proved; replace them (decide, norm_num, a proof)")
        elif not errors and phase(st) == 1 and not any(s for _, _, s in helpers) and not st.data.get("maps", {}).get(me) \
                and (not tgt or "sorryAx" in tgt["axioms"]):
            result["advice"] = ("no map yet: when you have a reduction, write the target's proof with the hard steps as helper "
                                "lemmas stated with sorry and run check — the team can then take the parts")
        result["team"] = render_delta(st, me) if deliver else ""
        write_index(st); write_updates(st)
    return result


def _declared(src: str, short: str) -> bool:
    return re.search(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*(?:theorem|lemma|def|abbrev|instance|structure|inductive|class)\s+(?:[\w'.]*\.)?" + re.escape(short) + r"(?![\w'])", src, re.M) is not None


def _insert_before_target(src: str, target: str, text: str) -> str:
    """Insert before the first declaration that still carries a sorry (a part being proved) or the target,
    whichever comes first, so that the pulled lemmas precede the proofs that use them."""
    lines = src.split("\n")
    pat = re.compile(r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma)\s+(?:[\w'.]*\.)?" + re.escape(target.split(".")[-1]) + r"\b")
    idx = next((i for i, line in enumerate(lines) if pat.match(line)), None)
    if idx is None:
        raise ValueError(f"target {target} not found in Problem.lean")
    orig_names = set()
    orig = Path(_ctx_get("COLLAB_WORKSPACE") or ".") / "Original.lean"
    if orig.exists():
        orig_names = {n for n, _, _ in declarations(orig.read_text())}
    decls = declarations(src)
    for n, t, s_ in decls:
        if s_ and n != target.split(".")[-1] and n not in orig_names:
            j = src.find(t)
            if j >= 0:
                first = src.count("\n", 0, j)
                k = next((i for i in range(first, len(lines)) if DECL_RE.match(lines[i])), first)
                if k < idx:
                    idx = k
                break
    # never above a declaration of this file that the pulled text refers to
    words = set(re.findall(r"[A-Za-z_][\w'.?!]*", text))
    for n, t, s_ in decls:
        if n in words and not s_:
            j = src.find(t)
            if j >= 0:
                end_line = src.count("\n", 0, j + len(t)) + 1
                if end_line > idx:
                    idx = end_line
    start = idx
    while start > 0 and lines[start - 1].strip().startswith("@["):
        start -= 1
    if start > 0 and lines[start - 1].rstrip().endswith("-/"):
        j = start - 1
        while j >= 0 and not lines[j].lstrip().startswith("/-"):   # the comment block right above, whatever kind
            j -= 1
        if j >= 0 and lines[j].lstrip().startswith("/--"):   # only a docstring belongs to the declaration
            start = j
    # keep `open ... in` modifiers attached to the target
    while start > 0 and re.match(r"^\s*(open|set_option|omit|include)\b.*\bin\s*$", lines[start - 1]):
        start -= 1
    return "\n".join(lines[:start] + text.rstrip("\n").split("\n") + [""] + lines[start:])


def pull(name: str) -> dict:
    """Insert a verified library lemma (with the library lemmas it uses) into my Problem.lean before the target."""
    with _workspace_lock(_dir(), _env("COLLAB_WORKSPACE")):   # v12.2: the read-insert-write of Problem.lean is one step
        return _pull(name)


def _pull(name: str) -> dict:
    """Insert a verified library lemma (with the library lemmas it uses) into my Problem.lean before the target."""
    d = _dir(); me = _me()
    problem = Path(_env("COLLAB_WORKSPACE")) / "Problem.lean"; src = problem.read_text()
    with _State(d) as st:
        e = st.data["lemmas"].get(name)
        if e is None and name in st.data.get("aliases", {}):
            name = st.data["aliases"][name]; e = st.data["lemmas"].get(name)
        if e is None:
            part = next((g for g in st.data["subgoals"].values() if g["name"] == name.split(".")[-1] and g["status"] == "open"), None)
            if part:
                return {"ok": False, "inserted": [], "note": f"{name} is an open part, not yet proved by anyone (on it: {', '.join(part['on']) or 'nobody'}); its statement:\n{part['statement']}",
                        "team": render_delta(st, me)}
            names = ", ".join(st.data["lemmas"])
            raise ValueError(f"no verified lemma named {name}; the library has: {names or 'nothing yet'}")
        needed, queue = [], [name]
        while queue:
            n = queue.pop()
            for dep in st.data["lemmas"][n]["deps"]:
                if dep in st.data["lemmas"] and dep not in needed and not _declared(src, dep.split("__")[0]):
                    needed.append(dep); queue.append(dep)
        # simp lemmas about the definitions the pulled lemmas use travel with them (a `simp` proof relies on them without naming them)
        defs = {n for n in needed + [name] for n in [n] if ":" in st.data["lemmas"][n]["typehash"]}
        defs |= {x for n in needed + [name] for x in st.data["lemmas"][n]["deps"] if x in st.data["lemmas"] and ":" in st.data["lemmas"][x]["typehash"]}
        for k, e in st.data["lemmas"].items():
            if k == name or k in needed or e.get("dup_of"):
                continue
            txt = e.get("text", "")
            if "@[simp" in txt[:40] and any(re.search(r"(?<![\w'])" + re.escape(x.split("__")[0]) + r"(?![\w'])", e["statement"]) for x in defs) \
                    and not _declared(src, k.split("__")[0]):
                needed.append(k)
        needed = [n for n in st.data["lemmas"] if n in needed and n != name]   # library order; never the lemma itself
        st.event(kind="pull", by=me, name=name)
        pl = st.agent(me).setdefault("pulled", [])
        for n in needed + [name]:
            if n.split("__")[0] not in pl: pl.append(n.split("__")[0])
        team = render_delta(st, me)
    short = name.split("__")[0]
    if _declared(src, short):
        own = next(((n, t, s_) for n, t, s_ in declarations(src) if n == short), None)
        if own and own[2]:   # my own sorry version of this statement (a part of my map): replaced by the verified one
            block = "".join((d / "verified" / file_of(n)).read_text().rstrip("\n") + "\n\n" for n in needed) + (d / "verified" / file_of(name)).read_text().rstrip("\n")
            problem.write_text(src.replace(own[1], block, 1))
            return {"ok": True, "inserted": needed + [name], "source": block, "note": f"your sorry declaration of {short} was replaced by the verified one", "team": team}
        return {"ok": True, "inserted": [], "note": "Problem.lean already declares this name; nothing inserted", "team": team}
    block = "".join((d / "verified" / file_of(n)).read_text().rstrip("\n") + "\n\n" for n in needed) + (d / "verified" / file_of(name)).read_text()
    problem.write_text(_insert_before_target(src, _env("COLLAB_TARGET"), block))
    warn = []
    with _State(d) as st:
        author = st.data["lemmas"][name]["by"]
        for dep in st.data["lemmas"][name]["deps"]:
            v = st.data["lemmas"].get(f"{dep}__{author}")
            if v and dep in st.data["lemmas"] and st.data["lemmas"][dep]["by"] != author:
                warn.append(f"{name} was verified against {author}'s own `{dep}` (in the library as {dep}__{author}), which differs from the library's `{dep}` by {st.data['lemmas'][dep]['by']}; if the proof breaks, pull {dep}__{author} and rename")
    res = {"ok": True, "inserted": needed + [name], "source": block, "note": "inserted before your first open part (or the target)", "team": team}
    if warn: res["warning"] = warn
    return res


def refute(name: str, evidence: str) -> dict:
    """Record that a subgoal or a claim is false, with the evidence (a counterexample, a computation)."""
    d = _dir(); me = _me()
    if not evidence or len(evidence) > MAX_EVIDENCE:
        raise ValueError(f"give the evidence in at most {MAX_EVIDENCE} characters")
    with _State(d) as st:
        hits = [s for s in st.data["subgoals"].values() if s["name"] == name.split(".")[-1]]
        for s in hits:
            s["status"] = "refuted"; s["evidence"] = evidence; s["refuted_by"] = me
        st.event(kind="refuted", by=me, name=name, evidence=evidence)
        team = render_delta(st, me); write_index(st); write_updates(st)
    return {"ok": True, "name": name, "matched_subgoals": len(hits), "team": team}


def say(type: str, text: str, ref: str = "") -> dict:
    """Post to the channel: idea, plan, feedback (needs a ref) or request. At most MAX_MESSAGE characters."""
    d = _dir(); me = _me(); claim_notes = []
    if type not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if not text or len(text) > MAX_MESSAGE:
        raise ValueError(f"the message must be 1–{MAX_MESSAGE} characters")
    with _State(d) as st:
        recent = [m for m in st.data["messages"] if m["by"] == me and time.time() - m["t"] < 600]
        if len(recent) >= 6:
            raise ValueError("rate limit: at most 6 messages per 10 minutes")
        m = dict(id=len(st.data["messages"]) + 1, by=me, type=type, text=text, ref=ref or None, t=time.time())
        st.data["messages"].append(m)
        with open(d / "channel.jsonl", "a") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        if type == "idea" and not st.agent(me).get("plan"):
            st.agent(me)["route"] = text   # a first idea counts as the agent's declared route
        if type == "plan":
            st.agent(me)["plan"] = text
            open_parts = [sg for sg in st.data["subgoals"].values() if sg["status"] == "open"]
            def self_claim(sg):
                for m in re.finditer(r"(?<![\w'])" + re.escape(sg["name"]) + r"(?![\w'])", text):
                    before = text[max(0, m.start() - 40):m.start()]
                    if not re.search(r"\bA[1-9]\b", before.replace(me, "")):
                        return True
                return False
            named = [sg for sg in open_parts if ref and sg["name"] == ref.split(".")[-1]]
            named += [sg for sg in open_parts if sg not in named and self_claim(sg)]
            named = named[:3]
            if named:   # claiming parts by name (ref, or the names in the text); my other claims are released
                for sg in open_parts:
                    if sg not in named and me in sg["on"]:
                        sg["on"].remove(me)
                        if not sg.get("notes", {}).get(me):
                            claim_notes.append(f"you left {sg['name']} without a note: `say note ref={sg['name']}` with the approach you tried, "
                                               f"what you proved and the goal you could not close — the next agent starts from it")
                for sg in named:
                    others = [o for o in sg["on"] if o != me]
                    sg.setdefault("approaches", {})[me] = text   # every claim carries its approach; shown once the part is hard
                    if is_hard(sg):   # a hard part takes any number of agents, each with a declared approach
                        mine_w = set(re.findall(r"[a-z]{4,}", text.lower()))
                        for o, txt in sg["approaches"].items():
                            if o == me:
                                continue
                            ow = set(re.findall(r"[a-z]{4,}", txt.lower()))
                            if mine_w and ow and len(mine_w & ow) / len(mine_w | ow) > 0.5:
                                claim_notes.append(f"{sg['name']}: your approach reads like {o}'s ({_short(txt)[:120]}); a parallel attempt pays only if it differs")
                        if me not in sg["on"]:
                            sg["on"].append(me); sg.setdefault("claimed", {}).setdefault(me, time.time())
                            st.event(kind="working", by=me, name=sg["name"])
                            st.agent(me)["working_on"] = sg["name"]
                        continue
                    if len(others) >= 2:
                        claim_notes.append(f"{sg['name']} already has {', '.join(others)} on it — enough people; you are not registered on it, take another part")
                        continue
                    if others:
                        since = min(sg.get("claimed", {}).get(o, sg["t_open"]) for o in others)
                        claim_notes.append(f"{sg['name']}: {', '.join(others)} on it for {_age(since)}"
                                           + (" — take a different approach from theirs" if time.time() - since >= STUCK_MIN * 60 else " — a second agent is only useful with a different approach"))
                    if me not in sg["on"]:
                        sg["on"].append(me); sg.setdefault("claimed", {}).setdefault(me, time.time())
                        st.event(kind="working", by=me, name=sg["name"])
                        st.agent(me)["working_on"] = sg["name"]
        if type == "note":
            if not ref:
                raise ValueError("a note is about a part: give its name as ref")
            hit = [s for s in st.data["subgoals"].values() if s["name"] == ref.split(".")[-1]]
            if not hit:
                raise ValueError(f"no part named {ref}; notes go on parts (open or closed)")
            for s in hit:
                s.setdefault("notes", {}).setdefault(me, []).append(dict(t=time.time(), text=text))
        if ref:
            for r in st.data["messages"]:
                if r["type"] == "request" and str(r["id"]) == ref:
                    r["answered"] = True
        st.event(kind="message", by=me, type=type, text=text, ref=ref or None)
        team = render_delta(st, me); write_index(st); write_updates(st)
    res = {"ok": True, "id": m["id"], "team": team}
    if claim_notes: res["claims"] = claim_notes
    if type in ("plan", "idea") and not st.data.get("maps"):
        res["next"] = ("write the route as a map now: the target's proof in Problem.lean with its steps stated as helper lemmas "
                       "with sorry, then check — that is the form of the plan the team can work on")
    return res


# ---------- the shared plan (H5) ----------
def _node_key(name: str) -> str:
    return "plan:" + hashlib.sha1(name.encode()).hexdigest()[:12]


def _find_node(st: "_State", name: str):
    short = name.split(".")[-1]
    for key, g in st.data["subgoals"].items():
        if g["name"] == short:
            return key, g
    return None, None


def plan(action: str, name: str = "", statement: str = "", parent: str = "", agent: str = "", text: str = "", children: str = "") -> dict:
    """Edit the shared plan: add a node (a sub-problem, with its statement in words or in Lean), take or drop
    it, split it into children, link it under a parent, or assign it to an agent. The harness binds nodes to
    the sorry helpers and verified lemmas of the files by name or statement."""
    d = _dir(); me = _me(); notes = []
    if action not in ("add", "take", "drop", "split", "link", "assign", "show"):
        raise ValueError("action must be one of add, take, drop, split, link, assign, show")
    with _State(d) as st:
        if action == "add":
            if not name or not statement:
                raise ValueError("add needs a name and a statement (in words or in Lean)")
            key, g = _find_node(st, name)
            if g is None:
                key = _node_key(name)
                g = st.data["subgoals"][key] = dict(name=name.split(".")[-1], statement=_short(statement), status="open", files=[], on=[],
                                                    t_open=time.time(), proposed_by=me)
                st.event(kind="open", by=me, name=g["name"], statement=g["statement"])
            else:
                notes.append(f"{name} already exists ({g['status']})")
            if parent:
                pk, pg = _find_node(st, parent)
                if pg is None:
                    notes.append(f"parent {parent} is not a node; add it first")
                else:
                    g["parent"] = pk; pg.setdefault("children", [])
                    if key not in pg["children"]: pg["children"].append(key)
        elif action == "take":
            key, g = _find_node(st, name)
            if g is None or g["status"] != "open":
                raise ValueError(f"{name} is not an open node")
            others = [o for o in g["on"] if o != me]
            if len(others) >= 2 and not is_hard(g):
                notes.append(f"{g['name']} already has {', '.join(others)} on it — enough people; not registered")
            else:
                for og in st.data["subgoals"].values():
                    if og is not g and me in og["on"]:
                        og["on"].remove(me); notes.append(f"released {og['name']}")
                if me not in g["on"]:
                    g["on"].append(me); g.setdefault("claimed", {}).setdefault(me, time.time())
                    st.event(kind="working", by=me, name=g["name"])
                st.agent(me)["working_on"] = g["name"]
                if text and is_hard(g):
                    g.setdefault("approaches", {})[me] = text
        elif action == "drop":
            key, g = _find_node(st, name)
            if g is None:
                raise ValueError(f"no node named {name}")
            if me in g["on"]: g["on"].remove(me)
            if text:
                g.setdefault("notes", {}).setdefault(me, []).append(dict(t=time.time(), text=text))
            else:
                notes.append("leave a note with the drop: what you tried, what you proved, where it failed")
            st.event(kind="left", by=me, name=g["name"])
        elif action == "split":
            key, g = _find_node(st, name)
            if g is None:
                raise ValueError(f"no node named {name}")
            kids = [c.strip() for c in re.split(r"[;\n]", children) if c.strip()]
            if not kids:
                raise ValueError("split needs children: 'name1: statement1; name2: statement2'")
            g.setdefault("children", []); g["split_by"] = me
            for c in kids:
                cn, _, cs = c.partition(":")
                cn = cn.strip(); cs = cs.strip() or cn
                ck, cg = _find_node(st, cn)
                if cg is None:
                    ck = _node_key(cn)
                    cg = st.data["subgoals"][ck] = dict(name=cn, statement=_short(cs), status="open", files=[], on=[], t_open=time.time(), proposed_by=me)
                    st.event(kind="open", by=me, name=cn, statement=cg["statement"])
                cg["parent"] = key
                if ck not in g["children"]: g["children"].append(ck)
            st.event(kind="split", by=me, name=g["name"], parts=[c.partition(":")[0].strip() for c in kids])
        elif action == "link":
            key, g = _find_node(st, name); pk, pg = _find_node(st, parent)
            if g is None or pg is None:
                raise ValueError("link needs an existing node and an existing parent")
            g["parent"] = pk; pg.setdefault("children", [])
            if key not in pg["children"]: pg["children"].append(key)
        elif action == "assign":
            key, g = _find_node(st, name)
            if g is None:
                raise ValueError(f"no node named {name}")
            g["suggested"] = agent or me
        team = render_delta(st, me); write_index(st); write_updates(st)
        tree = plan_tree(st)
    res = {"ok": True, "plan": tree, "team": team}
    if notes: res["notes"] = notes
    return res


def plan_tree(st: "_State") -> str:
    """The whole plan as an indented tree: roots are the maps' parts and unlinked nodes."""
    sg = st.data["subgoals"]
    by_key = {k: g for k, g in sg.items() if g["status"] in ("open", "closed", "refuted")}
    roots = [k for k, g in by_key.items() if not g.get("parent") or g["parent"] not in by_key]
    lines = []
    def emit(k, depth):
        g = by_key[k]
        st_ = {"open": ("open; " + (", ".join(g["on"]) + " on it" if g["on"] else "nobody on it")), "closed": f"proved by {g.get('proved_by', '?')}", "refuted": "refuted"}[g["status"]]
        extra = ("; proposed by " + g["proposed_by"] + (", not yet stated in Lean" if not g.get("files") and g["status"] == "open" else "")) if g.get("proposed_by") else ""
        extra += f"; suggested for {g['suggested']}" if g.get("suggested") else ""
        lines.append("  " * depth + f"- {g['name']} ({st_}{extra}): {_short(g['statement'])[:160]}")
        for ck in g.get("children", []):
            if ck in by_key:
                emit(ck, depth + 1)
    for k in roots:
        emit(k, 0)
    map_parts = {x for m in st.data.get("maps", {}).values() for x in m.get("parts", [])}
    def root_of(k):
        seen = set()
        while by_key[k].get("parent") in by_key and k not in seen:
            seen.add(k); k = by_key[k]["parent"]
        return k
    orphans = [g["name"] for k, g in by_key.items() if g["status"] == "open" and map_parts and by_key[root_of(k)]["name"] not in map_parts] if map_parts else []
    if orphans:
        lines.append("not linked to any parent (say which part they serve: plan link <name> <parent>): " + ", ".join(orphans))
    return "\n".join(lines) if lines else "(empty)"


# ---------- launcher side ----------
def resume_text(collab_dir: str, who: str) -> str:
    """The resume message for an agent that ended its session: the delta plus concrete open subgoals."""
    with _State(Path(collab_dir)) as st:
        delta = render_delta(st, who, mark_seen=True)
        open_ = [s for s in st.data["subgoals"].values() if s["status"] == "open"]
        open_.sort(key=lambda s: (len(s["on"]), s.get("disputed", False), s["t_open"]))
        ph = phase_lines(st, who)
    lines = ["The target is not proved yet. Continue working on it."]
    if delta:
        lines.append(delta)   # the delta carries the header and the parts
    else:
        lines += ph
    return "\n".join(lines)
