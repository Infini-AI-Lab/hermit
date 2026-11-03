---
name: reproduce-method
description: Implement or review a published method in Hermit using the method reproduction guide and checklist.
---

# Reproduce a method

## Understand the method

Read the [reproduction guide](../../../docs/developer/reproducing-a-method.md)
and the authors' implementation. Identify the learned adapter, the Evolver's
inputs and update rules, and how the final adapter is selected.

Map the upstream components to Hermit and explain changes to prompts, evidence,
selection or supported capabilities. Use the guide's upstream-comparison table
to distinguish framework adaptations, task adaptations and reproduction choices.

## Implement and verify

Inspect the existing templates, Evolvers and relevant shared interfaces before
adding code. Follow the [three-layer architecture](../../../docs/framework/agent-layers.md)
and the guide's boundary between method code and core changes.

Follow the guide's verification sequence: focused tests, template/config checks,
a small real run with trajectory inspection, then the required evaluation.
Use the benchmark's shared settings and explain deviations. State what could
not be verified; keep the PR draft until the guide's first-evaluation requirement
is met.

## Prepare the contribution

Update the method documentation and examples with the implementation and results.
Complete the [method checklist](references/reproduction-checklist.md) alongside
the [general checklist](../prepare-pr/references/general-pr-checklist.md),
using links to the actual files and records.

Use [prepare-pr](../prepare-pr/SKILL.md) to put those answers in the PR.
