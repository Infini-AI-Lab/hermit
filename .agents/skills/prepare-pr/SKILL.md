---
name: prepare-pr
description: Draft or update a Hermit PR title and description using the repository template and applicable checklists.
---

# Prepare a pull request

## Establish the change

Read [CONTRIBUTING.md](../../../CONTRIBUTING.md), the
[PR template](../../../.github/PULL_REQUEST_TEMPLATE.md), and the complete change.
Include relevant local changes, preserving unrelated work. For an existing PR,
read its description and review feedback. Identify the problem, resulting behavior,
affected interfaces, and the validation evidence available.

## Complete the applicable checks

- Use the [general checklist](references/general-pr-checklist.md)
  for every PR, plus all applicable task-specific checklists.
- For method reproductions, also complete the
  [method checklist](../reproduce-method/references/reproduction-checklist.md).

Keep checklist questions in their owning files. Answer shared items once, using
actual code, checks and records rather than planned work.

## Write the PR

Write a short title describing the problem fixed or capability added. Keep the
template's sections and existing checkboxes:

- **Change:** explain the problem and resulting behavior, then the affected
  interfaces, core changes, documentation and important design decisions.
  Include the applicable task-specific answers, linking detailed documentation.
- **Validation:** list commands actually run and their results. Link experiment
  configurations and records where applicable. State failures, skipped checks,
  unverified behavior, and requirements for reproducing the checks.
- **Review:** add applicable checklist items not already covered by the template.
  Answer overlapping items in the existing entry rather than adding duplicates.
  Record the independent review's scope, findings, fixes and remaining concerns.
  Reference answers in `Change` or `Validation` rather than repeating them.

Only mark completed checks as complete; explain inapplicable items. Keep long
logs in links or collapsible details. If independent review will run after PR
creation, leave that item unchecked and state that it is pending.

## Read back and deliver

Check every path, name, number and claim against the final code and records.
After patches, update stale descriptions and identify any validation or review
that no longer covers the change.

Return the proposed title and body when a preview is requested. Commit, push or
publish only when included in the user's authorization.

After creating a PR or updating its code, follow
[post-pr-audit](../post-pr-audit/SKILL.md) for an independent review of the actual PR.
