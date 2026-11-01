# curate-brief — the briefing is a cache with eviction, not an archive

Read at agenda item 4.

## Facts about the reader

The briefing is prepended before the task; a host reads top-down and acts on
the first thing that tells it what to do. Everything else lives at /world-model and is
reached only if the host knows to look — the briefing's routing lines are how
it knows. Budget ~30000 chars. It also means hosts tend to execute every
sentence they read, hedges included — "do not assume X" reads to a host
as a prohibition on X.

## What the briefing must do

Updating it every session is not optional: a host reading a stale or
near-empty briefing works blind. Hosts receive it truncated at 30000
chars (a visible seam marks the cut) and the wrapper reports the file's
size with every session. It should be a ROUTE MAP rather than a dump —
what just changed, what bites most often, and what can be asked about.
Curate it: for each thing that recurs, the rule a host follows today —
one line, with the scope it holds in, its evidence extent and age, and
the cases behind it — then what remains untested, stated as a fact about
the evidence. Nothing speculative stated as fact; but a line that lists
observations without the rule they support is not finished. Every
line carries the tick it was last validated at — this file is where you
read your own model's age, and an undated line hides in it. Carry each
line's `derived_from` addresses on the line itself, in the record-address
form: a line whose address lives only in the entry file costs a host two
actions instead of one. Whether to follow an address is the host's own
call — you offer it, you do not require it. It must help ANY competent
agent, not one particular model.

## What goes in

- memory entries: one line each — the conclusion and the extent of its evidence
  (verified where / when; what remains untested), newest first. Describe uncertainty, staleness and missing
  evidence as facts about the evidence, not as advice about what the host
  should or should not do. Verified procedures and action-outcome
  relationships are welcome: when the action itself is what was tested,
  report what was done and what happened.
- procedures: one TRIGGER line each, routing to the memory entry
  or tool that carries the steps.
- tools/agents: one trigger line each, with the exact invocation.
- episodes: normally none — they are reached by grep, not by pre-reading.

## Eviction

Evicting is not deleting: the full set stays under /world-model. Prefer to evict what
is oldest and what a host could re-derive cheaply.
Before evicting the routing line of something expensive to miss, weigh
the turns a host would burn rediscovering it. Compression bias is real — a
briefing that keeps getting tightened for its own sake loses old routes
without anyone deciding to evict them; the fix is eviction to /world-model,
not tighter prose.

## Before finalizing: audit for unintended policy

Check whether any line turns an observation about uncertainty, staleness,
missing evidence, or limited evidence into advice, caution, prohibition,
or a requirement for the host. Remove such advisory language while
preserving the underlying evidence statement.

Also check both directions of scope distortion: do not turn "verified
here" into "valid only here" without evidence of failure elsewhere, and
do not turn a scoped observation into an unqualified general rule.

Do not remove a procedure or action merely because it may influence host
behavior. If the action itself was tested, the observed action and
outcome are evidence and should remain.

The audit changes how evidence is represented, not what strategy the
host should follow. Note in the journal what the audit changed.

These are considerations, not requirements. When your evidence argues
otherwise, follow the evidence and record the deviation in the journal.
