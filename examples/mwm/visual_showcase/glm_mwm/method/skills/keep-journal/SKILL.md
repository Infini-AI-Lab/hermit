# keep-journal — the memory system: a record that rolls, a docket that does not

Read at agenda item 1 (walking the docket) and at the close of every
session.

## The design, and why it is split

Session memory has two lifecycles, and one surface cannot serve both.
What happened this block is DONE the moment it is written — it only
needs to stay findable. What is still UNSETTLED must stay in front of
you until it settles — if open questions live inside session entries,
they roll into one-line digests with the entries, the archive is rarely
read back, and the same wrong reading gets paid for twice. So:

- `meta/journal.md` — the RECORD. Rolls: only the last five entries
  stay in full, older ones collapse to their Digest line.
- `meta/cases/` — the DOCKET. Never rolls: one file per open
  investigation, in front of every session until you delete it.

The wrapper reports both in every session prompt (docket census, sizes,
roll notices). You never need to remember that a case exists; you do
need to work it.

## The record: one entry per session

Head it `## Session <block>` with the bare number (`## Session 7`,
never `## Session block007` — a mislabeled heading never rolls). Then,
in this order:

1. `Digest: <one sentence>` — all that survives rolling, so make it
   carry the block's one load-bearing fact.
2. `update:` — for each episode whose outcome contradicts a served
   reading, what it showed as a fact with the conditions it covered, and
   what that changes in memory, with reasons.
3. This block's observations.
4. A one-line closure for each case settled this session.

Not: what was built or edited (git holds it), stamp-bump confirmations
(memory entries hold them), step-by-step narration, or anything still
open — open things get a case file, not a paragraph here.

## The docket: a case's life

OPEN one when this block's evidence cannot settle a failed
reading: name it NN-<slug>.md (id assigned once, never reused),
state what is open, the candidate readings with their status, and the
observation that would settle it — the case shape and what each outcome
would mean.

WORK the docket (at agenda item 1) every session: does this block's
evidence advance, settle, or retire any case? Rewrite the file in
place — it is the current state of the investigation, not its history.

PUT THE SETTLING OBSERVATION IN THE BRIEFING: an open case earns one
briefing line — what is still open, and the kind of episode that would
settle it, stated as facts (what each outcome would mean, never "you
should try this"). Hosts meet the environment every episode and you do
not; an episode that happens to land on that shape closes the case with
real evidence, at no cost to anyone. This is your main way of exploring
on purpose.

CLOSE it the moment it settles: the settlement goes to memory/ first,
one closure line goes into the session entry, then DELETE the file —
git keeps every version, deletion loses nothing. A settled case still
on the docket costs every future session's attention.

TRIAGE when the docket is full (12): close the least settleable case
first — the one no foreseeable case can decide — and merge cases the
same evidence would settle. Caps are enforced: an oversized case is
truncated in place by the wrapper (3000 chars), so tightest-current
beats complete-historical.

## What this system is not

Not a workspace for every-session operations, not a
home for settled facts (memory/), not a channel to hosts
(consumption/brief.md carries that). If a thing will never settle, it
is not a case — it is either a claim about the environment or nothing.
