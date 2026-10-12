# decide-executable — should this be a program or a delegate, and why

Read at agenda item 3, or whenever an executable form suggests itself.

## Why a tool wins, when it wins

Some work has an exact specification: verification, computation by fixed
rules, mechanical parsing or transformation. A model re-derives such work
every time and can slip; a program written once is exact, reproducible, and
free at use. When the correct behavior can be stated as input -> output, a
program is the more reliable executor, and the model's job shifts from doing
the work to deciding when to call it.

## When a sub-agent instead

A sub-problem that needs several steps of interaction with the
environment AND judgement along the way is neither a paragraph nor a
tool; /skills/create-subagent says when it deserves a sub-agent and how
to write one.

## Entry paths (no lineage requirement)

- exact-specification work observed in the evidence -> write the tool
  directly; its test.py is the admission, not prior life as prose.
- a procedure with judgment steps -> prose first (a memory entry);
  consider a program form later if its use recurs and its steps become
  scriptable.
- a recurring sub-problem needing environment interaction and judgement
  -> a sub-agent (/skills/create-subagent).
- unsure -> prose; it is cheap and fails soft.

Counter-shape: knowledge whose CONTRACT drifts (what "correct" means keeps
moving) stays prose, dated. The litmus for tool vs delegate: write the steps
as a script — a step that reads "look at it and decide" needs a model.

## Costs, stated plainly

A stale tool costs one failed self-check plus a fall back to its own
description; nothing worse — the runtime check refuses instead of lying.
A sub-agent is judged from the record after deployment (the turns it
took, its trajectory); its first episodes are that test.
Building nothing is a legitimate answer; say why in the journal.

These are considerations, not requirements. When your evidence argues
otherwise, follow the evidence and record the deviation in the journal.
