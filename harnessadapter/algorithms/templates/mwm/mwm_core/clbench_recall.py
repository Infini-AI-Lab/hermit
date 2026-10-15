"""The RECALL channel: hosts pull a case by its signature string.

Serving-side counterpart of the [[signature]] links the briefing carries.
A recall never touches the environment: it greps the world model's episode
entries for the verbatim signature and returns the entry whole. The venue's
query budget prices exploration of the database; reading one's own
accumulated knowledge is free, so recalls are not counted against it.

Resolution is literal. A miss returns a fixed one-line answer rather than a
near match — a fuzzy hit would teach the learner that sloppy signatures
still retrieve, and the signature discipline (author-knowledge: strings
that recur VERBATIM) is what keeps retrieval trustworthy.

Every recall is appended to a log beside the wrepo (uptake.jsonl) so a
later analysis can count how often the case library was actually reached
for, per signature, hit or miss. Nothing renders it to the learner.
"""

import json
import os
import time

# Where case entries live inside the wrepo. `examples/` is the V7 home — one
# file per graded episode, named for its address — and is tried FIRST, because
# an address is exact where a signature is a substring search. `episodes/`
# stays: world models created before V7 keep their cases there, and a repo
# with no examples/ resolves exactly as it did before.
EPISODE_DIRS = ("examples", "episodes")
NO_MATCH = "no case matches"


def _inside(wrepo: str, path: str) -> bool:
    """A host-side read stays inside the repository once links resolve
    (see wm_tools.inside): a planted link reads as absent."""
    root, p = os.path.realpath(wrepo), os.path.realpath(path)
    return p == root or p.startswith(root + os.sep)


def _by_address(wrepo: str, addr: str) -> str:
    """An example named for its address — examples/q17.md for `q17`.

    This is the resolution the V7 links are written against: the learner
    records `derived_from: q17`, the briefing carries the same string, and a
    host that follows it must land on that episode without a search. Matching
    the FILENAME is what makes the address exact; the text search below is the
    fallback for the older, signature-shaped links.
    """
    if "/" in addr or os.sep in addr or addr.startswith("."):
        return ""
    for sub in EPISODE_DIRS:
        path = os.path.join(wrepo, sub, addr + ".md")
        if os.path.isfile(path) and _inside(wrepo, path):
            try:
                return open(path).read()
            except OSError:
                return ""
    return ""


def resolve(wrepo: str, signature: str) -> str:
    """The example at that address, or the first entry containing the string."""
    sig = (signature or "").strip().strip("[]").strip()
    if not sig:
        return NO_MATCH
    hit = _by_address(wrepo, sig)
    if hit:
        return hit
    for sub in EPISODE_DIRS:
        root = os.path.join(wrepo, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name == "README.md":
                    continue
                path = os.path.join(dirpath, name)
                if not _inside(wrepo, path):
                    continue
                try:
                    text = open(path).read()
                except OSError:
                    continue
                if sig in text:
                    return text
                # The learner names a file after its signature and writes a
                # different, prose signature inside it; measured on one run,
                # 15 of 36 briefing links missed for exactly that reason.
                # The filename is a link the learner meant to make.
                if name.endswith(".md") and sig == name[:-3]:
                    return text
    return NO_MATCH


def log_recall(log_path: str, signature: str, hit: bool) -> None:
    """Append one recall event; failure to log must never fail the recall."""
    try:
        with open(log_path, "a") as f:
            f.write(json.dumps({"t": time.time(), "signature": signature,
                                "hit": hit}) + "\n")
    except OSError:
        pass


def normalize_address(signature: str) -> str:
    """The bare address from any form a host may pass: `q17`, `q17.md`,
    `examples/q17.md`, `/world-model/examples/q17.md` -> `q17`."""
    a = (signature or "").strip().strip("`'\"")
    a = a.split("/")[-1]
    if a.endswith(".md"):
        a = a[:-3]
    return a


def recall(wrepo: str, signature: str, log_path: str = "") -> str:
    """Resolve and log in one step — the entry point for the venue system."""
    signature = normalize_address(signature)
    text = resolve(wrepo, signature)
    if log_path:
        log_recall(log_path, signature, text != NO_MATCH)
    return text
