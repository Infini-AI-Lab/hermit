name: attached-delegation
when: delegated work comes back confidently wrong because the sub-task retold the situation instead of carrying the raw material
uses: delegation, large-text-input

## Applicable when

Evidence shows a sub-agent answering a paraphrased sub-task fluently but
wrongly: the retelling dropped or distorted a fact, and the sub-agent —
which sees the episode's input and the world model, but nothing of the
host's conversation — had no way to notice.

## Skeleton

The host's task text carries the material, verbatim:

```
task = "<what is wanted>"
       + "\n===== RAW MATERIAL: <name> (verbatim) =====\n" + body
       ...
```

Two clauses in the spec's prompt do the work:
1. trust the attached material over any retelling in the task text;
2. if the task statement contradicts the attachments, report the
   contradiction instead of silently resolving it.

Attach sources verbatim — the requester's exact words, tool outputs as
printed — never a summary of them. Where the sub-agent can reach the
environment itself, the prompt says which facts to re-check there rather
than take from the task text.

## Demo

See `demo/`. A grower's loss report is delegated for triage with the
nursery's germination log attached; the task statement misstates the
trays lost and where, and the delegate reports both contradictions
instead of triaging the misstated report. Without the attachment, the
same call gets a confident wrong triage. Request assembly and the two
prompt clauses are form (keep); log fields and report vocabulary are
content (replace). The delegate is a deterministic stub — the form is
the protocol, not the model.

## Integration

A spec whose prompt states the two clauses and names what must ride
along verbatim; the spec's first line (the catalog line hosts read)
says so too, since the host writes the task text.

## Instrumentation

Count delegations whose task text carried attachments (adoption) and
contradiction reports (catches — retellings were drifting). Zero
attachments means the habit did not take: revise the spec's first line,
not the form.
