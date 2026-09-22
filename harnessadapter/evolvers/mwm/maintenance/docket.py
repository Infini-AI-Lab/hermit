"""The docket (setting v1.10): meta/cases/, one open investigation per
file, never rolled. Restored from the v1.1 setting (V9.2 lineage) as the
learner's open-questions surface after the hypothesis mechanism was
retired — the v1.5 hypothesis files had absorbed it, and removing them
(v1.9) had left the learner with no surface that keeps an unsettled
question in front of every session.

Design, unchanged from v1.1:
  - one file per open case, NN-<slug>.md, two-digit id assigned once;
  - 3000 chars per case, ENFORCED in place before each session (git
    keeps the full text); at most 12 rendered;
  - the wrapper renders a census (name, size, cap flags) in every
    session prompt, so a case's existence never depends on the learner
    remembering it; deleting the file closes the case.
Nothing is settled, matched or verdict-ed by the wrapper: the docket is
a passive registry, which is what the v1.5–v1.7.1 measurements said an
open-question surface should be.

MW_DOCKET=0 removes it (the v1.9 form). When the hypothesis mechanism
is on (MW_HYPOTHESIS=1) the docket defaults to off, so the v1.5–v1.7.1
arms stay reproducible.
"""

from __future__ import annotations

import os
import re
from typing import List

from . import hypothesis as _hyp

ENABLED = os.environ.get("MW_DOCKET", "0" if _hyp.ENABLED else "1") == "1"

DIR = os.path.join("meta", "cases")
CASE_CAP = 3000
MAX_CASES = 12
_CASE_NAME = re.compile(r"^(\d{2})-[A-Za-z0-9._-]+\.md$")
_CASE_MARK = ("\n[wrapper-truncated at %d chars — rewrite this case "
              "tighter; git holds the full text]" % CASE_CAP)

README = """One OPEN CASE per file, named NN-<slug>.md (two-digit
id, assigned once; deleting the file closes the case and retires the
number). A case is a question awaiting settlement: an unresolved
interpretation, a candidate list under narrowing, a watch that a
specific future episode can settle. State what is open, the candidates
and their status, and what evidence would settle it. Rewrite in place
as the investigation moves; 3000 chars per case, ENFORCED — the wrapper
truncates an oversized case in place before each session (git keeps the
full text). At most 12 open cases: a full docket means close or merge
one first.

Closing a case: write the settlement where it belongs (memory/ for
facts), leave a one-line closure in the session's journal entry, then
DELETE the file — git keeps every version, deletion loses nothing.

How to run the docket — opening, advancing, the settling line in the
briefing, triage when full: /skills/keep-journal.

NOT for: operations to run every session, settled
facts (memory/), host-facing knowledge (ADAPTER.md), session
narration (meta/journal.md).
"""


def enforce_case_caps(wrepo: str) -> List[str]:
    """Truncate oversized case files IN PLACE before the session; returns
    the repo-relative paths it cut. git keeps the full version."""
    if not ENABLED:
        return []
    cdir = os.path.join(wrepo, DIR)
    if not os.path.isdir(cdir):
        return []
    cut = []
    for name in sorted(os.listdir(cdir)):
        if name == "README.md" or not _CASE_NAME.match(name):
            continue
        p = os.path.join(cdir, name)
        try:
            text = open(p, errors="replace").read()
        except OSError:
            continue
        if len(text) > CASE_CAP:
            open(p, "w").write(text[:CASE_CAP - len(_CASE_MARK)]
                               + _CASE_MARK)
            cut.append(f"{DIR}/{name}")
    return cut


def cases_report(wrepo: str) -> List[str]:
    """One line per open case — the wrapper-computed docket census."""
    cdir = os.path.join(wrepo, DIR)
    if not os.path.isdir(cdir):
        return ["(no cases directory)"]
    lines, matched = [], []
    for name in sorted(os.listdir(cdir)):
        if name == "README.md":
            continue
        if not _CASE_NAME.match(name):
            lines.append(f"IGNORED {DIR}/{name} (name must be NN-<slug>.md)")
            continue
        matched.append(name)
    for name in matched[:MAX_CASES]:
        try:
            text = open(os.path.join(cdir, name), errors="replace").read()
        except OSError:
            continue
        n = len(text.strip())
        flag = ""
        if text.rstrip().endswith(_CASE_MARK.strip()):
            flag = (" TRUNCATED by the wrapper before this session — "
                    "rewrite it tighter (git holds the full text)")
        elif n >= 0.8 * CASE_CAP:
            flag = " NEAR CAP"
        lines.append(f"{DIR}/{name}: {n}/{CASE_CAP} chars{flag}")
    for name in matched[MAX_CASES:]:
        lines.append(f"IGNORED {DIR}/{name} (over the {MAX_CASES}-case "
                     f"docket — close or merge one first)")
    return lines or ["(no open cases)"]


def render_status(wrepo: str) -> str:
    """The status-block section, or "" when the docket is off."""
    if not ENABLED:
        return ""
    return ("Open cases (meta/cases/ — your tracking surface; delete a "
            "case when it settles):\n" + "\n".join(cases_report(wrepo)))
