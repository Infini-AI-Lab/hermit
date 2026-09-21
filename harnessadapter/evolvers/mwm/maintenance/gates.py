"""Admission gates on the W repo, run by the wrapper AFTER each meta session
and BEFORE the mechanical commit. Fail-closed: any violation reverts the whole
session (git reset --hard) — CLAUDE.md warns the meta-agent about this.

Gates (v1):
  G1 entry schema: every memory/*.md has the four frontmatter fields
     (statement in the body), brief: within 300 chars. status/confidence/
     provenance were retired 2026-09-03 (memory holds facts; hypothesis
     bookkeeping lives in the ledger).
  (G2 was a brief-length gate. Removed: length is a QUOTA, not
   contamination — the wrapper truncates at render and reports the
   overrun, rather than throwing away a session's work.)
  G3 host-conditioning lexicon: brief + memory must not name host models or
     task ids (kimi, gpt, oss, claude, deepseek, task_id patterns).
  G4 no imports/exec in memory entries (plain text layer).
  G5 protected paths untouched by the session: evidence/, state.json
     (checked by the wrapper via git status, not here; bin/ became
     meta-writable by operator decision 2026-08-18).
  G6 hypothesis schema (v1.7): open files under meta/hypothesis/ carry
     opened_t/about/condition/prediction/derived_from; a verdict line
     carries `implies:`; opened_t is written once (restored if edited);
     deletions are restored.
"""

from __future__ import annotations

import glob
import os
import re
import sys
import types

import importlib.util

# Ported from metaworld mwm/gates.py (22b67ea). The architecture modules a gate
# needs (clbench_recall, capabilities, wm_tools) are imported from the
# framework's own copy of the mwm architecture, never from the candidate: the
# candidate's mwm_core is a protected path (evolvers/mwm/prompt.py
# PROTECTED_PATHS), so the framework copy IS the code that will serve, and no
# learner-written file is executed in the wrapper's process.
CAPTEST = 'test_python(directory="tools/<name>")'
_CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     "..", "architectures", "mwm", "mwm_core")


def _adapter_module(wrepo: str, name: str):
    core = os.path.normpath(_CORE)
    package = "_gates_mwm_core"
    if package not in sys.modules:
        pkg = types.ModuleType(package); pkg.__path__ = [core]; sys.modules[package] = pkg
    full = package + "." + name
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, os.path.join(core, name + ".py"))
    module = importlib.util.module_from_spec(spec); sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


def normalize_address(*args, **kwargs):
    raise RuntimeError("normalize_address is resolved per adapter; use _normalize(wrepo, ...)")


def _normalize(wrepo, address):
    return _adapter_module(wrepo, "clbench_recall").normalize_address(address)
from typing import List

# The briefing budget is NOT enforced here. Two constants used to sit at this
# spot, BRIEF_MAX_CHARS and BRIEF_HARD_CHARS, the second commented as the point
# where "the wrapper actually truncates". Nothing read either one: the venue
# plugin renders consumption/brief.md in full, and no gate measures its length.
# The comment was therefore describing a truncation that does not happen —
# removed 2026-08-20 rather than left to mislead the next reader.
#
# The budget the learner actually obeys is the sentence in its own contract —
# since the 2026-08 slot consolidation it lives directly in
# mwm/contract_texts/core.md ("Budget 30000 chars"), one text for every
# venue. Change it there. Note that CONTRACT.md is baked
# into a world model when init_wrepo creates it, so editing the slot only
# affects world models created afterwards.
REQUIRED_FIELDS = ["last_confirmed_t", "scope", "derived_from", "brief"]
# Two of those carry a safeguard the whole world model leans on — the
# validation tick and the scope fence (claude-doc/design-invariants.md) — and
# they matter as much on an episode or a tool as on a claim. Only claims were
# ever checked, so a workflow with no scope line, or a tool nobody dated, went
# unmentioned. These two are checked on every type; brief stays claim-only,
# being the claim's own vocabulary.
UNIVERSAL_FIELDS = ["last_confirmed_t", "scope", "derived_from"]
# One sentence per field, because a finding that only names a missing key
# teaches the shape of the file and not the reason for it.
WHY = {
    "last_confirmed_t": "Every entry states when it was last validated; "
                        "without it a reader cannot tell whether to trust it "
                        "here and now.",
    "scope": "Every entry states where it does NOT hold; a rule with no "
             "fence gets applied where it was never tested.",
    "derived_from": "Every entry names the worked examples it came from, so "
                    "a host wanting more than the line can go read the "
                    "episode behind it. Nothing behind it? Write "
                    "'derived_from: none (reasoning only)'.",
}
# An address is a bare example filename: q17, h214, block003-census. Values
# that declare there is no example ("none", "self") are legitimate and are not
# addresses; anything with whitespace inside it is prose, not a filename.
_NOT_AN_ADDRESS = ("none", "self", "n/a", "-", "")


def _addresses(line: str) -> List[str]:
    """The addresses a `derived_from:` line names, prose tolerated."""
    val = line.split(":", 1)[1].strip()
    if val.lower().startswith(_NOT_AN_ADDRESS[:3]):
        return []
    out = []
    for part in val.split(","):
        tok = part.strip().strip("[]`").split(" ")[0].strip(".;")
        # A path form (examples/q17.md, /world-model/examples/q17.md)
        # names the same record as the bare address; check it too.
        tok = tok.split("/")[-1]
        if tok.endswith(".md"):
            tok = tok[:-3]
        if tok and tok.lower() not in _NOT_AN_ADDRESS:
            out.append(tok)
    return out


# (directory, document); "" means the .md files sit directly in the directory
ENTRY_TYPES = (("episodes", ""), ("examples", ""), ("workflows", "WORKFLOW.md"),
               ("tools", "TOOL.md"), ("agents", "SPEC.md"))
SIGNATURE = re.compile(r"\[\[([^\]]{2,})\]\]")
# `claude(?!\.md)` — the agent legitimately cites its own contract file
# CLAUDE.md; only host-MODEL names are host-conditioning.
HOST_LEXICON = re.compile(
    r"\b(kimi|gpt[- ]?oss|gpt-\d|claude(?!\.md)|deepseek|qwen|gemini)\b", re.I)
# The raw-identifier lint (TASKID_PAT) was removed 2026-09-05: its id shapes
# and its example sentence were venue-specific (retail order ids, banking
# ticket hashes, "a split-paid pending order") and it misfired on 10-digit
# Unix timestamps in the database venue. Citing evidence by tick is already
# the core contract's rule; no shared lint enforces a venue's id format.


CONTRACT_AGENT_FIELDS = {"id", "commit", "selector", "expect", "note"}


PER_CHECK_CAP = 4


def _bounded(errs: List[str], found: List[str], what: str) -> None:
    """Append at most PER_CHECK_CAP findings of one kind, then say how many
    were left out.

    The advice list is rendered under a cap of its own, so a check that fires
    once per entry — sixty episodes with no scope line — would otherwise fill
    every slot and hide the other checks. A learner shown one kind of defect
    fixes one kind of defect.
    """
    if not found:
        return
    errs.extend(found[:PER_CHECK_CAP])
    if len(found) > PER_CHECK_CAP:
        errs.append(f"... and {len(found) - PER_CHECK_CAP} more {what}.")


def _check_entry_fields(wrepo: str, errs: List[str]) -> None:
    """The tick and the scope fence, on every type that carries knowledge."""
    found: List[str] = []
    for sub, doc in ENTRY_TYPES:
        pat = (os.path.join(wrepo, sub, "*.md") if not doc
               else os.path.join(wrepo, sub, "*", doc))
        for path in sorted(glob.glob(pat)):
            if "_template" in path or os.path.basename(path) == "README.md":
                continue
            body = open(path, errors="replace").read()
            rel = os.path.relpath(path, wrepo)
            for name in UNIVERSAL_FIELDS:
                if not re.search(rf"^{name}:", body, re.M):
                    found.append(f"{rel}: no '{name}:' line. {WHY[name]}")
    _bounded(errs, found, "entries missing a required line")


def _check_derived_from(wrepo: str, errs: List[str]) -> None:
    """Do the addresses entries and the briefing carry reach an example?

    `derived_from` is the model's one link, and it is only worth its
    characters if following it lands somewhere. An address that resolves to
    nothing is worse than no address: it costs a host an action and returns a
    miss, and the host reads that as "the case library is empty".
    """
    root = os.path.join(wrepo, "examples")
    have = {os.path.basename(p)[:-3]
            for p in glob.glob(os.path.join(root, "*.md"))
            if "_template" not in p and os.path.basename(p) != "README.md"}
    # No early return when examples/ is empty. An address written before any
    # example exists is the same broken promise as one pointing at the wrong
    # file, and staying silent until the directory fills is how a link decays
    # unnoticed — the failure this whole mechanism exists to prevent.
    targets = [(os.path.relpath(p, wrepo), open(p, errors="replace").read())
               for p in sorted(glob.glob(os.path.join(wrepo, "*", "*.md")) +
                               glob.glob(os.path.join(wrepo, "*", "*", "*.md")))
               if "_template" not in p
               and os.path.basename(p) != "README.md"
               and os.sep + "examples" + os.sep not in p]
    brief_p = os.path.join(wrepo, "consumption", "brief.md")
    if os.path.exists(brief_p):
        targets.append(("brief", open(brief_p, errors="replace").read()))
    found: List[str] = []
    for rel, body in targets:
        for line in re.findall(r"^derived_from:.*$", body, re.M):
            for addr in _addresses(line):
                if addr not in have:
                    found.append(
                        f"{rel}: derived_from names {addr}, and "
                        f"examples/{addr}.md does not exist — the address is "
                        f"the FILENAME under examples/.")
    _bounded(errs, found, "addresses that reach no example")


# Citation forms in briefing prose. `[id]` / `[[id]]` name a memory entry
# or an example; examples/q17.md is the path form; a bare address (q17) is
# what core.md calls followable and is checked as a whole word.
_BRACKET_CITE = re.compile(r"\[\[?([A-Za-z0-9_.-]{2,})\]\]?")
_PATH_CITE = re.compile(r"(?:/world-model/)?examples/([A-Za-z0-9_.-]+)\.md")


def _check_recall_index(wrepo: str, errs: List[str]) -> None:
    """Do the examples the briefing cites exist, and does it cite any?

    The briefing is the only surface every host reads, so an example the
    briefing never cites is unreachable in practice however well it is
    written, and a citation that resolves to nothing costs a host a call and
    reads as "the library is empty". A host follows an `[id]` with wm memory
    and a bare address (or its examples/<address>.md path) with wm recall.
    """
    brief_p = os.path.join(wrepo, "consumption", "brief.md")
    if not os.path.exists(brief_p):
        return
    brief = open(brief_p, errors="replace").read()

    def _stems(d):
        return {os.path.basename(p)[:-3]
                for p in glob.glob(os.path.join(wrepo, d, "*.md"))
                if "_template" not in p and os.path.basename(p) != "README.md"}
    examples, entries = _stems("examples"), _stems("memory")
    if not examples:
        return
    cited = sorted(set(_BRACKET_CITE.findall(brief))
                   | {_normalize(wrepo, c) for c in _PATH_CITE.findall(brief)})
    found = [f"brief: cites [{c}], and neither examples/{c}.md nor "
             f"memory/{c}.md exists."
             for c in cited if c not in examples and c not in entries]
    _bounded(errs, found, "briefing citations that resolve to nothing")
    if not any(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(e) + r"(?![A-Za-z0-9_-])", brief)
               for e in examples):
        errs.append(
            f"brief: {len(examples)} examples on disk and the briefing cites "
            f"none of them — hosts cannot recall what the briefing never "
            f"cites (cite the address, e.g. {sorted(examples)[0]}).")


# Findings shown to the next session; the rest are counted. Dropped by
# mistake in 0644273 (2026-09-07) while the renderer kept using it: the
# first session to produce a finding then killed the run with a NameError
# (v26m_flash_order0 at session 5, v26m_glm53_order0 at session 2).
ADVICE_CAP = 12


def render_advice(advice: List[str]) -> str:
    """The previous session's gate findings, for the next session's prompt.

    check() has always produced these and session.py has always returned them
    in report["advice"] — and for the whole life of the venue plugins nothing
    read that key, so every finding was computed and dropped. A world model
    could carry an entry with no scope line, or a briefing pointing at cases
    that do not resolve, and the one actor able to repair it was never told.
    """
    if not advice:
        return "Repository check on your last session: nothing to report."
    head = advice[:ADVICE_CAP]
    more = len(advice) - len(head)
    lines = ["Repository check on your last session — findings, not verdicts; "
             "agenda item 1 is where they belong:"]
    lines += [f"  - {a}" for a in head]
    if more:
        lines.append(f"  ... and {more} more of the same kinds.")
    return "\n".join(lines)



def _check_hypotheses(wrepo: str, errs: List[str]) -> None:
    """v1.5: schema of open hypotheses and the once-written opened_t stamp.
    Deletions are restored in session._finish; edits to opened_t are
    restored here and reported."""
    from . import hypothesis as _H
    errs.extend(_H.gate(wrepo))


def check(wrepo: str) -> List[str]:
    """Everything this returns is ADVICE, reported to the next session.

    The wrapper used to revert a whole session for any of these. Measured over
    137 sessions that fired four times, every one of them a format or
    procedure defect rather than an attempt to game the benchmark: a first
    session that had not learned the claim schema yet, and three that deleted
    their own predictions under a rule the instructions never stated.

    None of them can move the score. The score is computed host-side and
    written outside this repository; the meta-agent cannot reach it. Raw
    identifiers were checked directly — the order numbers in the serving
    stream and in the test tickets do not intersect at all, so an id-keyed
    lookup table wins nothing. So these are reported and left to the agent to
    act on. The one thing still enforced lives in session._finish(): reading
    files outside the repository, which is the only action that changes what
    the score means.
    """
    errs = []
    _check_hypotheses(wrepo, errs)
    _check_entry_fields(wrepo, errs)
    _check_recall_index(wrepo, errs)
    _check_derived_from(wrepo, errs)
    _C = _adapter_module(wrepo, "capabilities")
    if not hasattr(_C, "gate"):   # native capabilities.gate is a no-op; a port without it means the same
        _C = type("_NoGate", (), {"gate": staticmethod(lambda wrepo: [])})
    errs += _C.gate(wrepo)
    if not os.path.exists(os.path.join(wrepo, "consumption", "brief.md")):
        errs.append("consumption/brief.md deleted — hosts read it every episode")
    _wmt = _adapter_module(wrepo, "wm_tools"); BRIEF_MAX_CHARS = _wmt.BRIEF_MAX_CHARS; memory_dir = _wmt.memory_dir
    cdir = str(memory_dir(wrepo))
    mname = os.path.basename(cdir) if os.path.isdir(cdir) else "memory"
    if not os.path.isdir(cdir):
        # A deleted memory/ is a legal write (the dir is the learner's) and
        # a finding, never a wrapper crash — the fail-closed gate must
        # report it, not traceback on the listing.
        errs.append(f"{mname}/ deleted — the hosts' memory catalog serves "
                    f"from it every episode")
    found: List[str] = []
    for fn in (sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []):
        # README.md is the directory's announcement to the hosts, not a claim.
        # Checked as one, it produced five findings every session about a file
        # that is not supposed to carry those fields — noise that pushes real
        # findings past the cap.
        if not fn.endswith(".md") or fn.startswith("_") or fn == "README.md":
            continue
        body = open(os.path.join(cdir, fn)).read()
        for f in REQUIRED_FIELDS:
            if not re.search(rf"^{f}:", body, re.M):
                errs.append(f"{mname}/{fn}: missing required field '{f}:'. Every entry needs all four: last_confirmed_t, scope, derived_from, brief (a fresh one-line summary, 300 chars max — hosts pick entries to read by it).")
        bm = re.search(r"^brief:\s*(.*)$", body, re.M)
        if bm and len(bm.group(1).strip()) > BRIEF_MAX_CHARS:
            errs.append(f"{mname}/{fn}: brief is {len(bm.group(1).strip())} chars; the catalog serves at most {BRIEF_MAX_CHARS}. Rewrite it as a summary, not a truncation.")
        if HOST_LEXICON.search(body):
            errs.append(f"{mname}/{fn}: names a host model. Write what is true of the ENVIRONMENT, never what some model should do — this world model has to work for any agent.")
        if re.search(r"^\s*(import |def |subprocess|exec\()", body, re.M):
            captest = CAPTEST
            errs.append(f"{mname}/{fn}: contains code. {mname}/ is prose only — put programs in tools/<name>/run.py with a SCHEMA dict and a run() function, plus TOOL.md and test.py, then verify with: {captest} --wrepo . --name <name>")
    brief_p = os.path.join(wrepo, "consumption", "brief.md")
    if os.path.exists(brief_p):
        brief = open(brief_p).read()
        # Over-budget is NOT a session-killing violation any more. Reverting a
        # whole session of good edits because the brief ran 20 chars long is a
        # punishment wildly out of proportion to the defect, and the agent
        # cannot count characters precisely while writing markdown. The
        # wrapper truncates at render time instead and reports the overrun to
        # the next session, so the host still never sees more than the budget.
        if HOST_LEXICON.search(brief):
            errs.append("brief: names a host model")
    return errs
