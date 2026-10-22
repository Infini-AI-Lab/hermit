"""The hypothesis system (setting v1.7.1): what is not yet known about the
environment, one QUANTITY per file, in a form that says which future
case can constrain it.

meta/hypothesis/<id>.md:

    opened_t: <tick; written once, never changed>
    about: <the ONE quantity this is about — a threshold, one branch's
            action, one branch's reason code, a precedence between two
            checks, a field's meaning>
    condition: <which cases constrain it, and what observation has
                discriminating power>
    prediction: <the current reading of that quantity — a bracket, a
                 candidate set, an order>
    derived_from: <evidence addresses>

    <body: why this was opened; candidate readings if several>

A hypothesis is a piece of knowledge about the WORLD, never a rule for
hosts. Rules (the briefing) are compiled from memory; memory carries
the working default (bracket midpoint, the action with a correct case
behind it). A hypothesis therefore has no `default` and no
`intervention` kind: "does my rule work" is not a proposition about
the environment.

Settling: the learner appends ONE verdict line —

    verdict: confirmed|narrowed|excluded @ t=<tick> | fact: <what the
    case showed, as a fact about the world with the states it covered>

— and the wrapper archives the file at commit time. The fact is the
verdict's first product; what it changes in memory is a separate
judgement the learner records under `update:` in the journal. No
verdict obliges further action. (Legacy `held`/`refuted` verdicts and
`implies:` tails are still read, so earlier world models archive.)

Capacity: OPEN_CAP open hypotheses are rendered, creation order;
beyond that the oldest are dormant — unlisted, still matchable.

Design: claude-doc/hypothesis-system-v15.md (v1.5 core) and the v1.6/
v1.7 deltas in claude-doc/frozen-setting-v1.md.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

# Ablation switch (v1.7.1-nohyp): MW_HYPOTHESIS=0 removes the whole
# mechanism — no seeding, no index, no open-questions block, no gate, no
# archiving. Uncertainty then lives inside memory entries and the
# briefing, as it did before v1.5.
# v1.8: OFF by default. The v1.5–v1.7.1 arms set MW_HYPOTHESIS=1.
ENABLED = os.environ.get("MW_HYPOTHESIS", "0") == "1"

DIR = os.path.join("meta", "hypothesis")
ARCHIVE = os.path.join(DIR, "_archive")
INDEX = os.path.join(ARCHIVE, "INDEX.md")
OPEN_CAP = 100

REQUIRED = ("opened_t", "about", "condition", "prediction", "derived_from")
VERDICTS = ("confirmed", "narrowed", "excluded")
_LEGACY = {"held": "confirmed", "refuted": "excluded"}

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.md$")
_FIELD = re.compile(r"^([a-z_]+):\s*(.*)$")
_VERDICT = re.compile(
    r"^\s*verdict:\s*(confirmed|narrowed|excluded|held|refuted)\s*@\s*t\s*=\s*"
    r"(\d+)(?:\s*\.\.\s*\d+)?\s*(.*)$", re.M | re.I)

README = """One OPEN HYPOTHESIS per file, <id>.md — about ONE quantity of the
environment: a threshold, one branch's action, one branch's reason
code, a precedence between two checks, a field's meaning. This is
where what is NOT yet known lives; memory/ holds what is (the current
best reading, stated as fact, with the working default). Hosts never
read this directory: they read memory and the briefing; the wrapper
adds an "Open questions" section listing which quantities are still
under test.

A hypothesis is knowledge about the WORLD, never a rule for hosts. Not
"above threshold X, action A with reason R" (three quantities and a
rule) but three files: "the threshold lies in (a, b]", "the action
above it is one of {A, B}; A has a correct case", "the reason above it
is R; R' is excluded". A verdict on one leaves the other two untouched.

Header, all fields required:

    opened_t: <tick when opened — written once, never changed>
    about: <the one quantity>
    condition: <which cases constrain it, and what observation has
                discriminating power>
    prediction: <the current reading: a bracket, a candidate set, an
                 order>
    derived_from: <evidence addresses, e.g. e41, e88>

    <body: why this was opened; candidate readings if several>

Settling: when a case of the block constrains the quantity, read it
and append ONE line —

    verdict: confirmed @ t=<tick> | fact: <what the case showed, with the states it covered>
    verdict: narrowed @ t=<tick> | fact: <...>
    verdict: excluded @ t=<tick> | fact: <...>

The fact is the first product of a verdict: a statement about the
world, with its states — "action A with reason R was wrong for
request kind K at attribute values v1–v2 (e<n>, e<m>)". It goes into
the memory entry's evidence as written. What it
changes is a second, separate question, answered in the journal under
`update:` with reasons: which reading moves and how far, which do not,
and why. A zero says the submitted reading did not hold in the state
observed; which remaining reading deserves to serve is a judgement
about support (a correct case, the document, merely not excluded).
A case carries several attributes; which of them decided the outcome
is also a judgement — splitting a reading by an attribute
and extending it to unseen attributes are both claims worth their
evidence. A verdict asks nothing further. When the quantity is still
open, open a new file with the narrowed prediction.

The wrapper archives settled files at commit time. Never edit
opened_t; never delete a file — a hypothesis leaves this directory
only through a verdict. The open list holds 100 in creation order;
older ones are dormant (unlisted, still matchable). Check
_archive/INDEX.md before opening one, so a reading already excluded
is not proposed again.
"""

INDEX_HEAD = ("# Closed hypotheses — one line each (id · verdict · about · "
              "fact). Full text: git history of meta/hypothesis/<id>.md.\n\n")


# --------------------------------------------------------------- parsing

def parse(text: str) -> Tuple[Dict[str, str], str]:
    """Header fields and body. The header is the leading run of
    `key: value` lines (blank lines tolerated); the first other line
    ends it."""
    fields: Dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        m = _FIELD.match(lines[i])
        if not m:
            break
        fields[m.group(1)] = m.group(2).strip()
        i += 1
    return fields, "\n".join(lines[i:])


def _split_tail(tail: str) -> Tuple[str, str]:
    """(scope, implies) out of `| scope: ... | implies: ...`; a legacy
    `— evidence` tail lands in scope."""
    scope = implies = ""
    parts = [p.strip() for p in re.split(r"\s*\|\s*", tail) if p.strip()]
    for p in parts:
        low = p.lower()
        if low.startswith("scope:"):
            scope = p[6:].strip()
        elif low.startswith("implies:"):
            implies = p[8:].strip()
        elif low.startswith("fact:"):
            implies = p[5:].strip()
        else:
            scope = scope or p.lstrip("—–- ").strip()
    return scope, implies


def verdict_of(text: str) -> Optional[Tuple[str, int, str, str]]:
    """(verdict, t, scope, implies) for the LAST verdict line, or None."""
    ms = list(_VERDICT.finditer(text))
    if not ms:
        return None
    m = ms[-1]
    v = m.group(1).lower()
    v = _LEGACY.get(v, v)
    scope, implies = _split_tail(m.group(3) or "")
    return v, int(m.group(2)), scope, implies


def _opened_t(fields: Dict[str, str]) -> int:
    try:
        return int(str(fields.get("opened_t", "")).strip())
    except ValueError:
        return -1


def entries(wrepo: str) -> List[Dict]:
    d = os.path.join(wrepo, DIR)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if fn == "README.md" or fn.startswith("_") or not fn.endswith(".md"):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, errors="replace").read()
        except OSError:
            continue
        fields, body = parse(text)
        problems = []
        if not _ID.match(fn):
            problems.append("file name must be <id>.md (letters, digits, "
                            ". _ -; 80 chars max)")
        for f in REQUIRED:
            if not fields.get(f):
                problems.append(f"missing `{f}:`")
        if fields.get("opened_t") and _opened_t(fields) < 0:
            problems.append("opened_t must be an integer tick")
        v = verdict_of(text)
        if v and not v[3]:
            problems.append("verdict line has no `fact:` — what the case "
                            "showed, as a fact with the states it covered")
        out.append({"id": fn[:-3], "path": p, "fields": fields,
                    "body": body, "verdict": v,
                    "opened_t": _opened_t(fields), "problems": problems})
    return out


def open_and_dormant(wrepo: str, cap: int = OPEN_CAP):
    live = [e for e in entries(wrepo) if e["verdict"] is None]
    live.sort(key=lambda e: (e["opened_t"], e["id"]))
    if len(live) <= cap:
        return live, []
    return live[-cap:], live[:-cap]


# -------------------------------------------------------------- rendering

def _one_line(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def render_index(wrepo: str, now_t: int, block_t_range=None) -> str:
    """Open hypotheses one per line (oldest first), dormant count, and
    the per-case matching declaration."""
    if not ENABLED:
        return ""
    opened, dormant = open_and_dormant(wrepo)
    lines = []
    if not opened and not dormant:
        lines.append("Open hypotheses: none. (meta/hypothesis/README.md "
                     "says what one is and when to open one.)")
    else:
        lines.append(f"Open hypotheses ({len(opened)} listed"
                     + (f", {len(dormant)} dormant — oldest beyond the "
                        f"{OPEN_CAP}-cap; still matchable" if dormant else "")
                     + "; oldest first; age in ticks since opened):")
        for e in opened:
            f = e["fields"]
            age = (now_t - e["opened_t"]) if e["opened_t"] >= 0 else "?"
            lines.append(f"  {e['id']}  [age {age}]  "
                         f"{_one_line(f.get('about', ''), 70)} — "
                         f"{_one_line(f.get('condition', ''), 120)}")
        for e in opened:
            if e["problems"]:
                lines.append(f"  ! {e['id']}: " + "; ".join(e["problems"]))
    rng = ""
    if block_t_range and block_t_range[1] > 0:
        rng = f" (t={block_t_range[0]}..{block_t_range[1]})"
    lines.append(
        f"Matching: for each case of this block{rng}, one journal line — "
        f"`match: e<n> -> <hypothesis id>` or `match: e<n> -> none`. A "
        f"matched hypothesis gets a `verdict:` line in its file "
        f"(confirmed | narrowed | excluded, with the `fact:` the case "
        f"showed and the states it covered; format in "
        f"meta/hypothesis/README.md); what that fact changes is answered "
        f"separately under `update:` in the journal, with reasons.")
    return "\n".join(lines)


def render_open_questions(wrepo: str, cap: int = 30) -> str:
    """Host-facing: which quantities are still under test and which case
    shapes would settle them. Working defaults live in memory, so none
    are repeated here. Newest first."""
    if not ENABLED:
        # v1.8: open questions live inside memory entries as `open:` lines;
        # the wrapper compiles them for the hosts.
        from .wm_tools import open_questions
        return open_questions(wrepo)
    opened, _ = open_and_dormant(wrepo)
    ok = [e for e in opened if not e["problems"]]
    if not ok:
        return ""
    ok.sort(key=lambda e: (-e["opened_t"], e["id"]))
    lines = []
    for e in ok[:cap]:
        f = e["fields"]
        lines.append(f"- {_one_line(f['about'], 90)}: current reading "
                     f"{_one_line(f['prediction'], 140)}. Settled by: "
                     f"{_one_line(f['condition'], 160)} "
                     f"[{_one_line(f['derived_from'], 50)}]")
    return "\n".join(lines)


# ---------------------------------------------------------------- archive

def archive_settled(wrepo: str, block: int) -> List[str]:
    if not ENABLED:
        return []
    done = []
    adir = os.path.join(wrepo, ARCHIVE)
    idx = os.path.join(wrepo, INDEX)
    for e in entries(wrepo):
        if e["verdict"] is None:
            continue
        os.makedirs(adir, exist_ok=True)
        verdict, t, scope, implies = e["verdict"]
        f = e["fields"]
        dest = os.path.join(adir, e["id"] + ".md")
        if os.path.exists(dest):
            dest = os.path.join(adir, f"{e['id']}@t{t}.md")
        os.replace(e["path"], dest)
        if not os.path.exists(idx):
            open(idx, "w").write(INDEX_HEAD)
        with open(idx, "a") as fh:
            fh.write(f"- {e['id']} · {verdict}@t={t} (block {block}) · "
                     f"{_one_line(f.get('about', ''), 90)}"
                     + (f" · fact: {_one_line(implies, 140)}"
                        if implies else "")
                     + (f" · scope: {_one_line(scope, 80)}" if scope else "")
                     + "\n")
        done.append(e["id"])
    return done


_IDX_ROW = re.compile(
    r"^- (\S+) · (?:\S+ · )?(confirmed|narrowed|excluded|held|refuted)@t=(\d+) "
    r"\(block (\d+)\)(?: · (.*))?$")


def _index_rows(wrepo: str) -> List[Dict]:
    idx = os.path.join(wrepo, INDEX)
    if not os.path.exists(idx):
        return []
    rows = []
    try:
        lines = open(idx, errors="replace").read().splitlines()
    except OSError:
        return []
    for ln in lines:
        m = _IDX_ROW.match(ln)
        if not m:
            continue
        rest = m.group(5) or ""
        implies = scope = ""
        mi = re.search(r"· (?:implies|fact): (.*?)(?: · scope: (.*))?$", rest)
        if mi:
            implies, scope = mi.group(1), (mi.group(2) or "")
        rows.append({"id": m.group(1),
                     "verdict": _LEGACY.get(m.group(2), m.group(2)),
                     "t": int(m.group(3)), "block": int(m.group(4)),
                     "implies": implies, "scope": scope})
    return rows


def last_session_verdicts(wrepo: str) -> List[Dict]:
    """Verdicts archived by the most recent block."""
    rows = _index_rows(wrepo)
    if not rows:
        return []
    last = rows[-1]["block"]
    return [r for r in rows if r["block"] == last]


def refuted_last_session(wrepo: str) -> List[str]:
    """Ids excluded in the previous block. v1.7 attaches no obligation."""
    return [r["id"] for r in last_session_verdicts(wrepo)
            if r["verdict"] == "excluded"]


# ------------------------------------------------------------------- gate

def gate(wrepo: str) -> List[str]:
    """Advisory findings: schema slips, verdicts without `implies`, and
    opened_t edits (restored)."""
    import subprocess
    errs: List[str] = []
    if not ENABLED:
        return errs
    d = os.path.join(wrepo, DIR)
    if not os.path.isdir(d):
        errs.append("meta/hypothesis/ deleted — the open questions the "
                    "hosts see are generated from it")
        return errs
    for e in entries(wrepo):
        for p in e["problems"]:
            errs.append(f"meta/hypothesis/{e['id']}.md: {p}")
        r = subprocess.run(["git", "show", f"HEAD:{DIR}/{e['id']}.md"],
                           cwd=wrepo, capture_output=True, text=True)
        if r.returncode == 0:
            old, _ = parse(r.stdout)
            if old.get("opened_t") and e["fields"].get("opened_t") \
                    and old["opened_t"] != e["fields"]["opened_t"]:
                errs.append(f"meta/hypothesis/{e['id']}.md: opened_t changed "
                            f"({old['opened_t']} -> "
                            f"{e['fields']['opened_t']}); it is written "
                            f"once. Restored.")
                text = open(e["path"]).read()
                text = re.sub(r"^opened_t:.*$",
                              f"opened_t: {old['opened_t']}", text,
                              count=1, flags=re.M)
                open(e["path"], "w").write(text)
    return errs


def restore_deleted(wrepo: str) -> List[str]:
    import subprocess
    if not ENABLED:
        return []
    r = subprocess.run(["git", "ls-tree", "--name-only", "HEAD", DIR + "/"],
                       cwd=wrepo, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    back = []
    for rel in r.stdout.split():
        fn = os.path.basename(rel)
        if fn == "README.md" or fn.startswith("_") or not fn.endswith(".md"):
            continue
        if os.path.exists(os.path.join(wrepo, rel)):
            continue
        subprocess.run(["git", "checkout", "HEAD", "--", rel], cwd=wrepo,
                       capture_output=True)
        back.append(fn[:-3])
    return back


def counts(wrepo: str) -> Dict[str, int]:
    opened, dormant = open_and_dormant(wrepo)
    out = {"open": len(opened), "dormant": len(dormant),
           "confirmed": 0, "narrowed": 0, "excluded": 0}
    for r in _index_rows(wrepo):
        out[r["verdict"]] += 1
    return out
