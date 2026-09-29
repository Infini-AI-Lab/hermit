"""The briefing size, advisory (v6).

V9.2 enforced a 30,000-char cap at the point of consumption: served()
cut the text and appended a visible seam. v6 (OfficeQA postmortem) has
no truncation anywhere in the harness: the venue serves the briefing
whole, and the wrapper's status block reports the file's size against
the advisory number every session so the learner can trim it. CAP keeps
its name for importers; it is a number to report against, not a cut.
"""

CAP = 30000


def served(text: str) -> str:
    """The briefing as a host receives it: unchanged, whatever its size."""
    return text
