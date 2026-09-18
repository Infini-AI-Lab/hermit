One OPEN CASE per file, named NN-<slug>.md (two-digit
id, assigned once; deleting the file closes the case and retires the
number). A case is a question awaiting settlement: an unresolved
interpretation, a candidate list under narrowing, a watch that a
specific future episode can settle. State what is open, the candidates
and their status, and what evidence would settle it. Rewrite in place
as the investigation moves; 3000 chars per case, ENFORCED — the wrapper
truncates an oversized case in place before each session (git keeps the
full text). At most 12 open cases: a full docket means close or merge
one first.

Closing a case: write the settlement where it belongs (memory/ for
facts), leave a one-line closure in the session's journal entry, then
DELETE the file — git keeps every version, deletion loses nothing.

How to run the docket — opening, advancing, the settling line in the
briefing, triage when full: /skills/keep-journal.

NOT for: operations to run every session, settled
facts (memory/), host-facing knowledge (consumption/brief.md), session
narration (meta/journal.md).
