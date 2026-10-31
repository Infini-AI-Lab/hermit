"""The briefing budget, enforced (V9.2).

For four generations the contract said "Budget 30000 chars, truncated
past that" while nothing measured or cut anything — v90 briefs drifted
to 30,170 chars against a promise with no enforcement behind it. The
budget is now real at the point of consumption: the venue serves the
briefing through served(), so a host never reads past the cap; the file
itself is left intact for the learner to trim (the wrapper's status
block reports the size every session).
"""

CAP = 30000
MARK = ("\n[briefing truncated at 30000 chars — trim consumption/"
        "brief.md; hosts never see what follows]")


def served(text: str) -> str:
    """The briefing as a host receives it: capped, with a visible seam."""
    if len(text) <= CAP:
        return text
    return text[:CAP - len(MARK)] + MARK
