"""v12.1: automatic assembly of a map. When every part of an agent's map is proved (a verified library lemma states
it), the harness builds the target's proof: the map's glue (the target's proof in the author's file, as of the
author's last map check) plus the library lemmas the parts and the glue need, in library order; it compiles the file
in the author's workspace (.assembly/) and, if the target is proved with clean axioms, writes it to the author's
Problem.lean, where the judge grades it. Sub-parts are not assembled (a part is proved by a lemma that states it).
Called by the reactions in the map author's agent context. Adapted from v51's collab.assemble."""
import os
import re
import sys
import time
from pathlib import Path

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collab as C


def _read_verified(d: Path, name: str) -> str:
    try:
        txt = (d / "verified" / C.file_of(name)).read_text()
    except OSError:
        return ""
    decls = C.declarations(txt)
    if len(decls) != 1:
        return txt
    _, t, _ = decls[0]
    try:
        i = txt.index(t.split("\n", 1)[0])
    except ValueError:
        return txt
    head = txt[:i]
    wrapped = head.lstrip().startswith(("section", "noncomputable section"))
    return head + t + ("\nend\n" if wrapped else "\n")


def _part_lemma(st, part: str):
    """The library lemma that proves the part named `part` (its subgoal is closed), or None."""
    for key, g in st.data["subgoals"].items():
        if g["name"] != part:
            continue
        if g["status"] != "closed":
            return None
        cands = [k for k, e in st.data["lemmas"].items() if not e.get("dup_of") and (e["typehash"] == key or C._short(e["statement"]) == C._short(g["statement"]))]
        return min(cands, key=lambda k: st.data["lemmas"][k]["t"]) if cands else None
    return None


def ready(st, author: str) -> bool:
    m = st.data.get("maps", {}).get(author) or {}
    if not m.get("glue") or not m.get("parts_now") or m.get("assembling") or m.get("assembled"):
        return False
    if m.get("assembly_failed_at") is not None and len(st.data["lemmas"]) <= m["assembly_failed_at"]:
        return False
    return all(_part_lemma(st, p) for p in m["parts_now"])


def assemble_map(collab_dir: str, author: str) -> dict:
    d = Path(collab_dir)
    with C._State(d) as st:
        if not ready(st, author):
            return {}
        m = st.data["maps"][author]; m["assembling"] = True
        parts = list(m["parts_now"]); glue = m["glue"]; ctx = m.get("ctx") or []
        part_lemma = {p: _part_lemma(st, p) for p in parts}
        lemmas = {k: dict(v) for k, v in st.data["lemmas"].items()}
    try:
        work = Path(C._env("COLLAB_WORKSPACE")); target = C._env("COLLAB_TARGET").split(".")[-1]
        orig = (work / "Original.lean").read_text()
        for p, ln in part_lemma.items():   # the glue calls each part by its name in the map; the library may hold it under another
            if ln != p:
                glue = re.sub(r"(?<![\w'.])" + re.escape(p) + r"(?![\w'])", ln, glue)
        short = {k.split("__")[0]: k for k in lemmas if not lemmas[k].get("dup_of")}
        queue = list(part_lemma.values()) + [short[w] for w in {w_.split(".")[-1] for w_ in re.findall(r"[A-Za-z_][\w'.]*", glue)} if w in short and w != target]
        needed = []
        while queue:
            n = queue.pop()
            if n in needed or n not in lemmas:
                continue
            needed.append(n); queue += lemmas[n].get("deps", [])
        orig_names = {n for n, _, _ in C.declarations(orig)}
        taken, order = set(orig_names) | {target}, []
        for n in [k for k in lemmas if k in needed]:   # library (verified) order: dependencies first; one declaration per name
            names = {x for x, _, _ in C.declarations(_read_verified(d, n))} or {n}
            if names & taken:
                continue
            taken |= names; order.append(n)
        block = "".join(re.sub(r"(?m)^(\s*)private\s+", r"\1", _read_verified(d, n)).rstrip("\n") + "\n\n" for n in order)
        tdecl = next((t for n, t, s in C.declarations(orig) if n == target), None)
        src = C._insert_before_target(orig.replace(tdecl, C.with_context(ctx, glue).rstrip("\n"), 1), target, block) if tdecl else None
        ok, err = False, "could not build the file"
        if src:
            adir = work / ".assembly" / "root"; adir.mkdir(parents=True, exist_ok=True)
            (adir / "Problem.lean").write_text(src)
            rc, out = C.compile_with_dump(adir / "Problem.lean", adir)
            errors = C.error_blocks(out); info = C.parse_dump(out) if not errors else {}
            ok = not errors and target in info and info[target]["axioms"] <= C.CLEAN_AXIOMS
            err = (errors[0] if errors else f"{target} depends on {sorted(info.get(target, {}).get('axioms', []))}")[:C.MAX_ERROR_NOTE]
    except Exception as exc:
        ok, err, src = False, f"assembly raised {exc!r}", None
    if ok:
        with C._workspace_lock(d, work):   # v12.2: never in the middle of a pull's read-insert-write
            (work / "Problem.lean").write_text(src)   # the judge grades this file
    with C._State(d) as st:
        m = st.data["maps"][author]; m["assembling"] = False
        if ok:
            m["assembled"] = time.time()
            st.event(kind="assembled", by=author, name=target, text="the target's proof assembled from the map's proved parts")
        else:
            m["assembly_failed_at"] = len(st.data["lemmas"]); m["assembly_failures"] = m.get("assembly_failures", 0) + 1
            m["assembly_error"] = C._short(err)[:600]
            st.event(kind="assembly_failed", by=author, name=target, text=m["assembly_error"])
    return {"ok": ok, "error": None if ok else err}
