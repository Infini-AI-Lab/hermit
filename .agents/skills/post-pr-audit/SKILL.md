---
name: post-pr-audit
description: Run an independent audit after creating a Hermit PR or updating its code, using the published PR and applicable checklists.
---

# Post-PR audit

This audit fulfills the independent review item in the general checklist.

## Establish the PR

Read the published PR's title, body, base, current head, full diff and existing
review feedback. Confirm that the code being reviewed matches that head.

## Request an independent review

Start an agent or session separate from the one that wrote the change. Give it
the PR URL and head, repository guidance, the
[general checklist](../prepare-pr/references/general-pr-checklist.md), and all
applicable task-specific checklists. For method reproductions, include the
[method checklist](../reproduce-method/references/reproduction-checklist.md).

Ask it to inspect the actual implementation, relevant callers, tests and linked
records against the PR's claims. Validate suspected problems where practical;
distinguish confirmed defects from questions or design tradeoffs. The author's
self-check is context, not proof that the change is correct.

## Report the outcome

Return findings ordered by severity, with file references, triggering conditions
and impact. Combine findings with the same root cause, and report the reviewed
head, checks performed and unverified areas. If the PR changed during review,
state which changes remain unaudited.

If no independent reviewer is available or the audit cannot complete, report it
as pending or incomplete. Do not mark independent review as passed. The author
records findings and their resolution in the PR when authorized to update it.

Report in the conversation unless posting a GitHub review is authorized. This
workflow does not authorize commits, pushes, approval or merge.
