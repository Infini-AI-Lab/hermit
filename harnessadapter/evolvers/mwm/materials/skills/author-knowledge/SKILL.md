# author-knowledge — writing memory entries that get found

Read when checklist item 2 has decided something is worth keeping.

## Shared discipline

- The FIRST LINE is the retrieval path. Write it in the words a future reader
  would search with, not the words that describe your session.
- State the claim's BOUNDARY — what it does NOT cover. Illustration: a
  claim states a rule and leaves open whether an adjacent case falls under
  it; the reader burns turns interpreting, and skips a cheap insurance
  action the rule seemed to make unnecessary — while a reader with no
  claim at all takes the insurance and passes. When two readings lead to
  different actions, a boundary sentence earns its keep.
- Knowledge must not talk a host out of cheap insurance. If an action costs
  little and hedges a risk (re-checking a value, re-running a verification), the
  entry may inform it, never discourage it.

## Per type

- memory entries: statement first, evidence-grade header. Demotion keeps the old
  value and its validity dates — what is demoted may hold again later.
- failure records in memory: when a failure has a verbatim error or
  symptom string, put it in the entry BODY unaltered — a later search
  for the symptom must land on the entry.
- procedures: a procedure worth keeping in prose is a memory entry —
  trigger phrasing in the brief:, numbered copy-paste steps in the
  body. A procedure with a track record and mechanically checkable
  steps belongs in tools/ as a program instead.
- lessons (memory/L-<slug>.md): one re-verified rung of a postmortem
  ladder (agenda item 6), with status:, scope: (table:<family>,
  convention:<name> or harness), step: (S1..S7), check: (the JSON list
  of cell/answer checks you ran — copied in, not summarised) and
  verified_by: postmortem:<nnn> beside the four header lines. Hosts see
  a lesson only while `status: verified`, which the controller decides
  by replaying check: every block with verify_cell / verify_answer; a
  claim with no replayable check is a memory entry, not a lesson. The
  brief: states the rule a host applies at that rung, in the question's
  own words (units, convention, vintage), 300 chars max.

These are considerations, not requirements. When your evidence argues
otherwise, follow the evidence and record the deviation in the journal.
