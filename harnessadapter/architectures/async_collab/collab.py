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
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

TYPES = ("idea", "plan", "feedback", "request")
MAX_MESSAGE = 300
DECL_RE = re.compile(r"^(?:@\[[^\]]*\]\s*)?(?:private\s+|protected\s+|noncomputable\s+)*(?:theorem|lemma|def|abbrev|instance)\s+([A-Za-z_][\w'.]*)", re.M)
AUX_RE = re.compile(r"^(match_\d+|proof_\d+|eq_\d+|_.*|\d+)$")
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
    logInfo m!"DECL {n} {kind} {(canon ci.type).hash} AX {ax} USES {used}"
"""


def _env(key):
    value = os.environ.get(key)
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
        return self.data["agents"].setdefault(who, {"last_seen": 0, "working_on": None, "plan": None, "idle": 0, "checks": 0, "decls": {}})


# ---------- Lean source handling ----------
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
            while j >= 0 and not lines[j].lstrip().startswith("/--"):
                j -= 1
            if j >= 0:
                start = j
        begins.append((start, name))
    out = []
    for k, (start, name) in enumerate(begins):
        end = begins[k + 1][0] if k + 1 < len(begins) else len(lines)
        text = "\n".join(lines[start:end]).rstrip("\n")
        out.append((name, text, re.search(r"\bsorry\b", text) is not None))
    return out


def header(text: str) -> str:
    """The statement: from the declaration keyword to `:=` (proof excluded), whitespace collapsed."""
    body = re.sub(r"/--.*?-/\s*", "", text, flags=re.S)
    body = re.sub(r"^@\[[^\]]*\]\s*", "", body.strip())
    i = body.find(":=")
    return re.sub(r"\s+", " ", body[:i] if i > 0 else body).strip()


def parse_dump(out: str) -> dict:
    res = {}
    for n, kind, th, ax, used in re.findall(r"DECL (\S+) (\S+) (\d+) AX \[([^\]]*)\] USES \[([^\]]*)\]", out, re.S):
        short = n.split(".")[-1]
        if AUX_RE.match(short):
            continue
        res[short] = dict(name=n, kind=kind, typehash=th, axioms={a.strip() for a in ax.split(",") if a.strip()},
                          uses={u.strip().split(".")[-1] for u in used.split(",") if u.strip()})
    return res


def compile_with_dump(problem: Path, work: Path, repo: str = None, lake: str = None) -> tuple:
    repo = repo or _env("COLLAB_REPO"); lake = lake or _env("COLLAB_LAKE")
    g = work / ".collab_check.lean"
    g.write_text(problem.read_text().rstrip() + "\n" + DUMP)
    p = subprocess.run([lake, "env", "lean", str(g)], cwd=repo, capture_output=True, text=True, timeout=540)
    out = (p.stdout + p.stderr).replace(str(g), "Problem.lean")
    return p.returncode, out


# ---------- the team delta ----------
def _short(s: str, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def render_delta(st: "_State", who: str, mark_seen: bool = True) -> str:
    a = st.agent(who)
    new = [e for e in st.data["events"] if e["seq"] > a["last_seen"] and e["by"] != who]
    lines = []
    verified = [e for e in new if e["kind"] == "verified" and e["by"] != who]
    for e in verified[-6:]:
        lines.append(f"new verified: {e['name']} ({e['by']}): {_short(e['statement'], 90)}    pull with: pull {e['name']}")
    if len(verified) > 6:
        lines.append(f"… and {len(verified) - 6} more verified lemmas, see index.md")
    open_nobody = [s for s in st.data["subgoals"].values() if s["status"] == "open" and not s["on"]]
    open_nobody.sort(key=lambda s: (s.get("disputed", False), s["t_open"]))
    if open_nobody and (new or a["checks"] <= 1):
        for s in open_nobody[:3]:
            lines.append(f"open, nobody on it: {s['name']} (from {', '.join(s['files'])}): {_short(s['statement'], 90)}")
    for e in [e for e in new if e["kind"] == "refuted"]:
        lines.append(f"refuted: {e['name']} — {e['by']}, {_short(e['evidence'], 80)}")
    if a.get("working_on"):
        others = [b for b, x in st.data["agents"].items() if b != who and x.get("working_on") == a["working_on"]]
        lines.append(f"you are on: {a['working_on']}" + (f" — {', '.join(others)} also on it" if others else ""))
    msgs = [e for e in new if e["kind"] == "message" and e["by"] != who]
    if msgs:
        lines.append("channel: " + " · ".join(f"[{e['type']}] {e['by']}: {_short(e['text'], 80)}" for e in msgs[-6:]))
    pending = [m for m in st.data["messages"] if m["type"] == "request" and not m.get("answered") and m["by"] != who]
    if pending and not msgs:
        lines.append("open requests: " + " · ".join(f"{m['by']}: {_short(m['text'], 60)}" for m in pending[-3:]))
    if not new and not (open_nobody and a["checks"] <= 1):
        return ""
    n_open = sum(1 for s in st.data["subgoals"].values() if s["status"] == "open")
    lines.append(f"full state: {len(st.data['lemmas'])} verified, {n_open} open, "
                 f"{sum(1 for s in st.data['subgoals'].values() if s['status'] == 'refuted')} refuted, "
                 f"{len(st.data['messages'])} messages — index.md in the library directory")
    if mark_seen:
        a["last_seen"] = st.data["seq"]
    return "[team since your last call]\n" + "\n".join(lines)


def write_updates(st: "_State"):
    """library/updates.md: the newest changes first (what every tool reply also carries), then what is open
    and who is on what. Agents read it with cat at the start of each work cycle."""
    d = st.d; t0 = st.data.setdefault("t0", time.time())
    def when(t): return f"{int((t - t0) // 60):3d} min"
    L = ["# Team updates (newest first)", ""]
    for e in reversed(st.data["events"][-40:]):
        k = e["kind"]
        if k == "verified":
            L.append(f"- {when(e['t'])}  verified: {e['name']} ({e['by']}): {_short(e['statement'], 100)}  → pull {e['name']}")
        elif k == "open":
            L.append(f"- {when(e['t'])}  new subgoal ({e['by']}): {e['name']}: {_short(e['statement'], 100)}")
        elif k == "refuted":
            L.append(f"- {when(e['t'])}  refuted: {e['name']} — {e['by']}: {_short(e['evidence'], 100)}")
        elif k == "message":
            L.append(f"- {when(e['t'])}  [{e['type']}] {e['by']}: {_short(e['text'], 120)}" + (f" (re {e['ref']})" if e.get('ref') else ""))
        elif k == "pull":
            L.append(f"- {when(e['t'])}  {e['by']} pulled {e['name']}")
        elif k == "closed":
            L.append(f"- {when(e['t'])}  subgoal closed: {e['name']} ({e['by']})")
    if len(L) == 2:
        L.append("- nothing yet")
    L += ["", "## Open subgoals nobody is on", ""]
    free = [g for g in st.data["subgoals"].values() if g["status"] == "open" and not g["on"]]
    L += [f"- {g['name']} (from {', '.join(g['files'])}): {_short(g['statement'], 120)}" for g in free] or ["- none"]
    L += ["", "## Who is on what", ""]
    L += [f"- {who}: {a.get('working_on') or '?'}" + (f" — plan: {a['plan']}" if a.get('plan') else "") for who, a in st.data["agents"].items()] or ["- nobody yet"]
    L += ["", "## Open requests", ""]
    req = [m for m in st.data["messages"] if m["type"] == "request" and not m.get("answered")]
    L += [f"- #{m['id']} {m['by']}: {m['text']}" for m in req] or ["- none"]
    (d / "updates.md").write_text("\n".join(L) + "\n")


def write_index(st: "_State"):
    d = st.d
    L = ["# Team library", "", "## Verified lemmas (pull <name> to use one)", ""]
    for name, e in st.data["lemmas"].items():
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
    p = subprocess.run([_env("COLLAB_LAKE"), "env", "lean", str(path)], cwd=_env("COLLAB_REPO"), capture_output=True, text=True, timeout=540)
    out = (p.stdout + p.stderr).replace(str(path), file)
    errors = [l for l in out.splitlines() if ": error" in l]
    with _State(_dir()) as st:
        team = render_delta(st, _me())
    return {"file": file, "compiled": p.returncode == 0 and not errors, "errors": errors[:8],
            "output": out[-3000:] if not errors else "", "team": team}


def run_check(d: Path, me: str, work: Path, target: str, repo: str, lake: str, deliver: bool) -> dict:
    """The body of `check`. The launcher also calls it on the host for every changed file (deliver=False): the
    library is filled whether or not the agent uses the tool; only a tool call marks the delta as seen."""
    d = Path(d); (d / "verified").mkdir(parents=True, exist_ok=True); work = Path(work); problem = work / "Problem.lean"
    src = problem.read_text()
    orig_names = {n for n, _, _ in declarations((work / "Original.lean").read_text())} if (work / "Original.lean").exists() else set()
    rc, out = compile_with_dump(problem, work, repo, lake)
    errors = [l for l in out.splitlines() if ": error" in l]
    info = parse_dump(out) if not errors else {}
    decls = declarations(src)
    helpers = [(n, t, s) for n, t, s in decls if n not in orig_names and n != target]
    tgt = info.get(target)
    result = {"compiled": not errors, "errors": errors[:8], "target_axioms": sorted(tgt["axioms"]) if tgt else None}
    with _State(d) as st:
        a = st.agent(me); a["checks"] += 1
        # working on: the last changed/new declaration that still has a sorry, else the erroring one
        prev = a.get("decls", {}); cur = {n: hashlib.sha1(t.encode()).hexdigest() for n, t, _ in decls}
        changed = [n for n, t, s in decls if cur[n] != prev.get(n)]
        mine = {h for h, _, _ in helpers} | {target}   # only helpers and the target count as "working on"
        cand = [n for n, t, s in decls if n in changed and n in mine and s]   # a declaration that still has a sorry
        working = cand[-1] if cand else None
        if working is None and errors:
            m = re.search(r"Problem\.lean:(\d+):", errors[0])
            if m:
                line = int(m.group(1)); pos = 0
                for n, t, s in decls:
                    i = src.find(t, pos)
                    if i >= 0 and src.count("\n", 0, i) + 1 <= line <= src.count("\n", 0, i + len(t)) + 1:
                        working = n; break
                    pos = max(pos, i)
        if working:
            if working != a.get("working_on"):
                st.event(kind="working", by=me, name=working)
            a["working_on"] = working; a["idle"] = 0
        else:
            a["idle"] += 1
            if a["idle"] >= 4:
                a["working_on"] = None
        a["decls"] = cur
        # verified helpers -> library (a name already there with the same statement is kept; a different one gets a suffix)
        newly = []
        if not errors:
            for n, t, s in helpers:
                ci = info.get(n)
                if s or ci is None or "sorryAx" in ci["axioms"] or ci["kind"] == "axiom":
                    continue
                key = n
                ex = st.data["lemmas"].get(n)
                if ex and ex["typehash"] != ci["typehash"]:
                    key = f"{n}__{me}"
                    ex = st.data["lemmas"].get(key)
                if ex:
                    continue
                deps = sorted(u for u in ci["uses"] if u in {h for h, _, _ in helpers} and u != n)
                (d / "verified" / file_of(key)).write_text(t.rstrip("\n") + "\n")
                st.data["lemmas"][key] = dict(name=key, by=me, statement=header(t), typehash=ci["typehash"], deps=deps, t=time.time())
                st.event(kind="verified", by=me, name=key, statement=header(t))
                newly.append(key)
            # open subgoals: sorry helpers, merged across agents by type hash (statement text when unknown)
            seen_keys = set()
            for n, t, s in helpers:
                if not s:
                    continue
                ci = info.get(n)
                key = ci["typehash"] if ci else hashlib.sha1(header(t).encode()).hexdigest()[:12]
                seen_keys.add(key)
                sg = st.data["subgoals"].get(key)
                if sg is None:
                    sg = st.data["subgoals"][key] = dict(name=n, statement=header(t), status="open", files=[], on=[], t_open=time.time())
                    st.event(kind="open", by=me, name=n, statement=header(t))
                if me not in sg["files"]:
                    sg["files"].append(me)
                if working == n and me not in sg["on"]:
                    sg["on"].append(me)
                if working != n and me in sg["on"]:
                    sg["on"].remove(me)
            for key, sg in st.data["subgoals"].items():
                if sg["status"] == "open" and me in sg["files"] and key not in seen_keys:
                    sg["files"].remove(me)
                    if me in sg["on"]:
                        sg["on"].remove(me)
                    if not sg["files"]:
                        sg["status"] = "closed"; st.event(kind="closed", by=me, name=sg["name"])
        result["verified_now"] = newly
        result["team"] = render_delta(st, me) if deliver else ""
        write_index(st); write_updates(st)
    return result


def _declared(src: str, short: str) -> bool:
    return re.search(r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma|def)\s+(?:[\w'.]*\.)?" + re.escape(short) + r"\b", src, re.M) is not None


def _insert_before_target(src: str, target: str, text: str) -> str:
    lines = src.split("\n")
    pat = re.compile(r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma)\s+(?:[\w'.]*\.)?" + re.escape(target) + r"\b")
    idx = next((i for i, line in enumerate(lines) if pat.match(line)), None)
    if idx is None:
        raise ValueError(f"target {target} not found in Problem.lean")
    start = idx
    while start > 0 and lines[start - 1].strip().startswith("@["):
        start -= 1
    if start > 0 and lines[start - 1].rstrip().endswith("-/"):
        j = start - 1
        while j >= 0 and not lines[j].lstrip().startswith("/--"):
            j -= 1
        if j >= 0:
            start = j
    # keep `open ... in` modifiers attached to the target
    while start > 0 and re.match(r"^\s*(open|set_option|omit|include)\b.*\bin\s*$", lines[start - 1]):
        start -= 1
    return "\n".join(lines[:start] + text.rstrip("\n").split("\n") + [""] + lines[start:])


def pull(name: str) -> dict:
    """Insert a verified library lemma (with the library lemmas it uses) into my Problem.lean before the target."""
    d = _dir(); me = _me()
    problem = Path(_env("COLLAB_WORKSPACE")) / "Problem.lean"; src = problem.read_text()
    with _State(d) as st:
        e = st.data["lemmas"].get(name)
        if e is None:
            names = ", ".join(list(st.data["lemmas"])[:20])
            raise ValueError(f"no verified lemma named {name}; the library has: {names or 'nothing yet'}")
        needed, queue = [], [name]
        while queue:
            n = queue.pop()
            for dep in st.data["lemmas"][n]["deps"]:
                if dep in st.data["lemmas"] and dep not in needed and not _declared(src, dep.split("__")[0]):
                    needed.append(dep); queue.append(dep)
        needed = [n for n in st.data["lemmas"] if n in needed]   # library order
        st.event(kind="pull", by=me, name=name)
        team = render_delta(st, me)
    if _declared(src, name.split("__")[0]):
        return {"ok": True, "inserted": [], "note": "Problem.lean already declares this name; nothing inserted", "team": team}
    block = "".join((d / "verified" / file_of(n)).read_text().rstrip("\n") + "\n\n" for n in needed) + (d / "verified" / file_of(name)).read_text()
    problem.write_text(_insert_before_target(src, _env("COLLAB_TARGET"), block))
    return {"ok": True, "inserted": needed + [name], "source": block, "note": "inserted before the target", "team": team}


def refute(name: str, evidence: str) -> dict:
    """Record that a subgoal or a claim is false, with the evidence (a counterexample, a computation)."""
    d = _dir(); me = _me()
    if not evidence or len(evidence) > 600:
        raise ValueError("give the evidence in at most 600 characters")
    with _State(d) as st:
        hits = [s for s in st.data["subgoals"].values() if s["name"] == name.split(".")[-1]]
        for s in hits:
            s["status"] = "refuted"; s["evidence"] = evidence; s["refuted_by"] = me
        st.event(kind="refuted", by=me, name=name, evidence=evidence)
        team = render_delta(st, me); write_index(st); write_updates(st)
    return {"ok": True, "name": name, "matched_subgoals": len(hits), "team": team}


def say(type: str, text: str, ref: str = "") -> dict:
    """Post to the channel: idea, plan, feedback (needs a ref) or request. At most 300 characters."""
    d = _dir(); me = _me()
    if type not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    if not text or len(text) > MAX_MESSAGE:
        raise ValueError(f"the message must be 1–{MAX_MESSAGE} characters; reference files or lemmas instead of pasting code")
    if type == "feedback" and not ref:
        raise ValueError("feedback needs a ref: the lemma, subgoal or message it is about")
    with _State(d) as st:
        recent = [m for m in st.data["messages"] if m["by"] == me and time.time() - m["t"] < 600]
        if len(recent) >= 6:
            raise ValueError("rate limit: at most 6 messages per 10 minutes")
        m = dict(id=len(st.data["messages"]) + 1, by=me, type=type, text=text, ref=ref or None, t=time.time())
        st.data["messages"].append(m)
        with open(d / "channel.jsonl", "a") as f:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
        if type == "plan":
            st.agent(me)["plan"] = text
        if type == "feedback" and ref:
            for s in st.data["subgoals"].values():
                if s["name"] == ref.split(".")[-1] and s["status"] == "open":
                    s["disputed"] = True
        if ref:
            for r in st.data["messages"]:
                if r["type"] == "request" and str(r["id"]) == ref:
                    r["answered"] = True
        st.event(kind="message", by=me, type=type, text=text, ref=ref or None)
        team = render_delta(st, me); write_index(st); write_updates(st)
    return {"ok": True, "id": m["id"], "team": team}


# ---------- launcher side ----------
def resume_text(collab_dir: str, who: str) -> str:
    """The resume message for an agent that ended its session: the delta plus concrete open subgoals."""
    with _State(Path(collab_dir)) as st:
        delta = render_delta(st, who, mark_seen=True)
        open_ = [s for s in st.data["subgoals"].values() if s["status"] == "open"]
        open_.sort(key=lambda s: (len(s["on"]), s.get("disputed", False), s["t_open"]))
    lines = ["The target is not proved yet. Continue working on it."]
    if delta:
        lines.append(delta)
    if open_:
        lines.append("Open subgoals in the library (statements of helpers that still carry a sorry in some teammate's file); pick one nobody is on, or refute it:")
        for s in open_[:5]:
            lines.append(f"- {s['name']} (on it: {', '.join(s['on']) or 'nobody'}): {_short(s['statement'], 120)}")
    return "\n".join(lines)
