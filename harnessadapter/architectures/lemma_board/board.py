"""The lemma board tools. State is a directory (LEMMA_BOARD_DIR) shared by all agents:

  board.json          one entry per lemma
  lemmas/<file>.lean  the submitted proof of a lemma
  verified/<file>.lean the accepted proof (written by the verifier)
  board.lock          taken for every write

The tools never run Lean. Verification of a submitted proof is the launcher's
job (a background verifier that sets status done or rejected).
"""
import fcntl
import json
import os
import re
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")
STATUSES = ("open", "claimed", "submitted", "done", "rejected", "blocked")


def _env(key):
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"{key} is not set for this agent")
    return value


def _dir() -> Path:
    d = Path(_env("LEMMA_BOARD_DIR"))
    d.mkdir(parents=True, exist_ok=True)
    (d / "lemmas").mkdir(exist_ok=True)
    (d / "verified").mkdir(exist_ok=True)
    return d


def _me() -> str:
    return _env("LEMMA_BOARD_AGENT")


def file_of(name: str) -> str:
    return name.replace("'", "_p").replace(".", "_") + ".lean"


class _Board:
    """Locked read-modify-write of board.json."""

    def __init__(self, d: Path):
        self.d = d
        self.path = d / "board.json"
        self.lock = open(d / "board.lock", "a+")

    def __enter__(self):
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"lemmas": []}
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            os.replace(tmp, self.path)
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def find(self, name):
        for e in self.data["lemmas"]:
            if e["name"] == name:
                return e
        return None


def _public(e: dict) -> dict:
    out = {k: e.get(k) for k in ("name", "statement", "status", "owner", "posted_by")}
    if e.get("reason"):
        out["reason"] = e["reason"]
    if e.get("error"):
        out["error"] = e["error"]
    return out


def board_list() -> dict:
    with _Board(_dir()) as b:
        entries = [_public(e) for e in b.data["lemmas"]]
    counts = {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES}
    return {"you": _me(), "counts": counts, "lemmas": entries}


def _tokens(statement: str) -> set:
    """Words of a statement's conclusion (after the last top-level colon), for near-duplicate detection."""
    concl = statement.rsplit(":", 1)[-1] if ":" in statement else statement
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_'.]*|[^\sA-Za-z0-9_]", concl))


def _similar(statement: str, others: list) -> list:
    mine = _tokens(statement)
    out = []
    for e in others:
        theirs = _tokens(e["statement"])
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= 0.6:
            out.append(e["name"])
    return out


def board_post(name: str, statement: str, claim: bool = True) -> dict:
    name = name.strip()
    if not NAME_RE.match(name):
        raise ValueError(f"not a Lean identifier: {name!r}")
    statement = statement.strip()
    if not statement or "sorry" in statement:
        raise ValueError("statement must be the declaration without proof")
    if name.split(".")[-1] not in statement:
        raise ValueError("statement does not mention the lemma name")
    me = _me()
    with _Board(_dir()) as b:
        if b.find(name):
            raise ValueError(f"a lemma named {name} is already on the board")
        e = {"name": name, "statement": statement, "status": "claimed" if claim else "open",
             "owner": me if claim else None, "posted_by": me, "time": time.time(),
             "history": [{"t": time.time(), "by": me, "event": "post", "claimed": bool(claim)}]}
        others = [_public(x) for x in b.data["lemmas"]]
        b.data["lemmas"].append(e)
    # the board as it was when this post landed: several agents post within the same minute,
    # so the poster sees at once whether someone else already has the same lemma
    return {"ok": True, "name": name, "status": e["status"], "already_on_board": others,
            "similar": _similar(statement, others)}


def board_claim(name: str) -> dict:
    me = _me()
    with _Board(_dir()) as b:
        e = b.find(name)
        if e is None:
            raise ValueError(f"no lemma named {name}")
        if e["status"] == "done":
            raise ValueError(f"{name} is already done; use board_pull")
        if e["status"] in ("claimed", "submitted") and e["owner"] != me:
            raise ValueError(f"{name} is owned by {e['owner']} ({e['status']})")
        e["status"], e["owner"] = "claimed", me
        e["history"].append({"t": time.time(), "by": me, "event": "claim"})
    return {"ok": True, "name": name, "status": "claimed"}


def board_done(name: str, proof: str) -> dict:
    me = _me()
    proof = proof.strip() + "\n"
    if re.search(r"\bsorry\b", proof):
        raise ValueError("the proof contains sorry")
    if not re.search(r"\b" + re.escape(name.split(".")[-1]) + r"\b", proof):
        raise ValueError("the proof text does not declare the lemma name")
    d = _dir()
    with _Board(d) as b:
        e = b.find(name)
        if e is None:
            raise ValueError(f"no lemma named {name}")
        if e["owner"] != me:
            raise ValueError(f"{name} is owned by {e['owner']}; claim it first")
        if e["status"] == "done":
            raise ValueError(f"{name} is already done")
        (d / "lemmas" / file_of(name)).write_text(proof)
        e["status"], e["error"] = "submitted", None
        e["history"].append({"t": time.time(), "by": me, "event": "submit"})
    return {"ok": True, "name": name, "status": "submitted",
            "note": "being compiled; board_list shows done or rejected when finished"}


def board_blocked(name: str, reason: str) -> dict:
    me = _me()
    with _Board(_dir()) as b:
        e = b.find(name)
        if e is None:
            raise ValueError(f"no lemma named {name}")
        if e["owner"] != me:
            raise ValueError(f"{name} is owned by {e['owner']}")
        if e["status"] == "done":
            raise ValueError(f"{name} is already done")
        e["status"], e["owner"], e["reason"] = "blocked", None, reason.strip()[:500]
        e["history"].append({"t": time.time(), "by": me, "event": "blocked", "reason": e["reason"]})
    return {"ok": True, "name": name, "status": "blocked"}


def _insert_before_target(src: str, target: str, text: str) -> str:
    """Insert text before the target's docstring / attribute block; the same
    place helper lemmas are allowed by the grader."""
    lines = src.split("\n")
    idx = None
    pat = re.compile(r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma)\s+(?:[\w'.]*\.)?" + re.escape(target) + r"\b")
    for i, line in enumerate(lines):
        if pat.match(line):
            idx = i
            break
    if idx is None:
        raise ValueError(f"target {target} not found in Problem.lean")
    start = idx
    while start > 0 and lines[start - 1].strip().startswith("@["):
        start -= 1
    # a docstring closes on the line before the attributes / declaration
    if start > 0 and lines[start - 1].rstrip().endswith("-/"):
        j = start - 1
        while j >= 0 and not lines[j].lstrip().startswith("/--"):
            j -= 1
        if j >= 0:
            start = j
    return "\n".join(lines[:start] + text.rstrip("\n").split("\n") + [""] + lines[start:])


def _declared(src: str, short: str) -> bool:
    return re.search(r"^\s*(?:private\s+|protected\s+)?(?:theorem|lemma|def)\s+(?:[\w'.]*\.)?" + re.escape(short) + r"\b", src, re.M) is not None


def board_pull(name: str) -> dict:
    """Insert the verified lemma before the target. Done lemmas accepted earlier whose names
    the pulled text uses, and that the file does not declare yet, go in first (in the order
    they were accepted), so that the file compiles in one pull."""
    d = _dir()
    with _Board(d) as b:
        e = b.find(name)
        if e is None:
            raise ValueError(f"no lemma named {name}")
        if e["status"] != "done":
            raise ValueError(f"{name} is {e['status']}, not done")
        e["history"].append({"t": time.time(), "by": _me(), "event": "pull"})
        earlier = sorted((x for x in b.data["lemmas"] if x["status"] == "done" and x.get("done_time", 0) < e.get("done_time", 0)),
                         key=lambda x: x.get("done_time", 0))
    text = (d / "verified" / file_of(name)).read_text()
    problem = Path(_env("LEMMA_BOARD_WORKSPACE")) / "Problem.lean"
    src = problem.read_text()
    short = name.split(".")[-1]
    if _declared(src, short):
        return {"ok": True, "name": name, "inserted": [], "source": text,
                "note": "Problem.lean already declares this name; nothing inserted"}
    # dependencies: earlier done lemmas mentioned by the pulled text (transitively), not yet in the file
    texts = {x["name"]: (d / "verified" / file_of(x["name"])).read_text() for x in earlier}
    needed, queue = [], [text]
    while queue:
        t = queue.pop()
        for x in earlier:
            s_ = x["name"].split(".")[-1]
            if x["name"] not in needed and re.search(r"\b" + re.escape(s_) + r"\b", t) and not _declared(src, s_):
                needed.append(x["name"]); queue.append(texts[x["name"]])
    needed.sort(key=lambda n: [x["name"] for x in earlier].index(n))
    block = "".join(texts[n].rstrip("\n") + "\n\n" for n in needed) + text
    problem.write_text(_insert_before_target(src, _env("LEMMA_BOARD_TARGET"), block))
    return {"ok": True, "name": name, "inserted": needed + [name], "source": text,
            "note": "inserted before the target" + (f", after its dependencies {needed}" if needed else "")}
