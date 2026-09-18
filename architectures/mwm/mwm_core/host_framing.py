"""What every mwm host reads above the briefing — one text for every venue.

B-7 (user decision 2026-09-06): the framing is method text (the host gets
a curated briefing instead of history; use it, but trust the observation
over it), so it is the same in every venue. Venue facts (scoring, opponent
names, what the desk is) are the task prompt's or the contract's (V2.3:
the database scoring sentence moved into the clbench question prompt).
"""

# 2026-09-08 (user decision): the framing no longer tells the host to skip
# exploration. The briefing adds experience the host lacks; it does not
# replace looking at the instance in front of it. Where a setting rewards
# saving actions, the meta can learn that and say so in the briefing — the
# framing itself stays neutral. (Trigger: v26m_flash hosts answered in 3
# turns on a briefing rule and lost q23/q33 where the instance or the
# wording had moved.)
TEXT = (
    "You are working in an environment you will encounter repeatedly. "
    "Earlier episodes are not in your context; instead you receive the "
    "briefing below, curated from them: what those episodes settled about "
    "this environment and what they left open. It adds to what you can "
    "find here; it does not replace looking at the instance in front of "
    "you. Confirm a briefing claim before you act on it, and treat a schema "
    "or a wording the briefing does not cover as unsettled. If an "
    "observation contradicts the briefing, trust the observation."
)
