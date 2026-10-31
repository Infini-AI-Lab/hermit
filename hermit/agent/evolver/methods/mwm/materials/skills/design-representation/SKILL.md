# design-representation — choose the shape a piece of knowledge should take

Read when checklist item 2 or 4 raises "what form should this take".

## Invariants (the interface the machinery depends on)

- Every artifact: first line routes; scope, last_confirmed_t, derived_from.
- Every type directory (memory/, examples/, tools/, agents/): README.md whose
  first line is its announcement.
- Underscore prefix = not part of the model.

## The choice is driven by properties of the knowledge, not by topic

| Ask | It decides |
|---|---|
| How often is it needed, and when? (before acting / during diagnosis / when a situation recurs) | briefing line vs memory lookup vs symptom search |
| How heavy is it? (one sentence vs multi-part with excerpts) | single file vs directory with references/ |
| How exposed to drift? (dated fact vs procedure stable across environment states) | how far down the crystallization ladder it may go |
| Can it be checked mechanically? | prose stays prose; only checkable things become tools |

Mechanical upgrade rule: a file becomes a directory when its entry outgrows
one screen or needs a second file. Not before.

## Known shapes (a catalog of options, not obligations)

single-file fact (memory/) · situation card with verbatim signatures
(a memory entry carrying verbatim signatures) · procedure as a memory entry or a tool · index + on-demand
references (any heavy object) · executable with self-check (tools/) ·
a sub-agent — a prompt run in the host's seat (agents/).

These are considerations, not requirements. When your evidence argues
otherwise, follow the evidence and record the deviation in the journal.
