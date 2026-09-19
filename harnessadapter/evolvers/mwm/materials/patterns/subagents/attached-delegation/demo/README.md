# Demo: loss-report triage with the germination log attached

`python3 test.py` — a grower's loss report is delegated for triage. The
task statement misstates the number of trays lost (9 vs the log's 2) and
the place (the cold frame vs the sowing bench). With the log attached, the
delegate holds the report and reports both contradictions; with only the
retelling, the same delegate confidently triages the wrong problem.

The delegate is a deterministic stub (`fake_delegate`) — the demo runs
fully offline. The form is the protocol around the call, not the model.

Form (keep when re-domaining): request assembly order (spec with a
`trigger:` first line, task, then `RAW MATERIAL` sections verbatim); the
two SPEC clauses — trust attachments over retellings, report
contradictions instead of resolving them.

Content (replace): the report vocabulary, the log fields, and which raw
files ride along for your recurring subtasks.
