# Prompt-only evolution guide

This architecture has one learned document, ADAPTER.md, served whole to the
acting host as its context. No adapter MCP server, tool or native subagent is
registered: tools stay empty and agents absent. This is the experiment's
chosen search space, not a framework restriction.

## ADAPTER.md sections

Keep the five headings; edit the bodies.

- `# Adapter context` — one paragraph on what the document is.
- `## Procedure` — the self-checks a host runs before committing an answer.
  Edit a check only when a verified lesson shows it wrong.
- `## Conventions` — one line per convention a family of questions uses
  (units, vintage, year basis, formula), with the evidence behind it.
- `## Lessons` — one entry per re-verified rung of a postmortem ladder
  (S1 vintage, S2 table, S3 units, S4 cells, S5 convention, S6 rounding,
  S7 harness), a `### L-<slug>` heading followed by key: value lines and
  a body:

      ### L-<slug>
      status: verified|candidate
      scope: table:<family>|convention:<name>|harness
      step: S1..S7
      check: <JSON list of {"kind":"cell","file","line","contains"} or {"kind":"answer","ordinal","candidate"}>
      verified_by: postmortem:<nnn>
      brief: <=300 chars — the rule a host applies at that rung

      <body: what was observed, the alternatives excluded, the addresses>

  The controller replays every lesson's check: list at the end of each
  block with the same two checkers you have (verify_cell, verify_answer)
  and rewrites its status: line in place: verified iff the list is
  non-empty and every check passes; an answer check on an episode not yet
  released fails. Hosts read every lesson in the file; a lesson with no
  replayable check is a convention, not a lesson.
- `## Open questions` — what the evidence left unsettled and the case shape
  that would settle it.

## Rules

- adapter.json and this file must not change; a session that changes either
  is not adopted.
- Nothing is cut: the host receives ADAPTER.md whole, and every episode
  record reports the bytes served (agents_md_bytes) against the host's
  limit (project_doc_max_bytes). Size is yours to manage.
- Use the evidence README to understand the records. Do not invent findings
  or turn one observed case into a universal rule; explain your changes in
  your report. Do not copy raw histories into the context.
- Framework infrastructure and evidence remain outside the candidate.
  Structural checks do not establish that new instructions improve task
  performance.
