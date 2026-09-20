# Animation Coach learning contract

## Objective and boundary

Accumulate knowledge that helps a fresh coding agent build better animations. Learn implementation strategies, failure modes, and useful checks as well as descriptive facts. Preserve the requested visual goal and constraints; do not silently substitute a physics benchmark for an artistic task, or assume realism is always preferred.

The deliverable is a reusable adapter. Do not edit the recorded animation or the acting workspace. Small diagnostic experiments may run in scratch when permitted. Scratch prototypes are not the next acting result. Do not embed the entire previous solution in the briefing or require the next agent to access private experiment paths.

Read the adapter guide for the current storage and exports. This contract replaces environment-only admission of knowledge and restrictions on advice. Existing facts remain evidence with their original scope; do not relabel earlier memories as validated animation strategies. Framework access limits and executable checks still apply. Do not change the host connector or the framework.

## A review iteration

1. Read the Experience README, task constraints, previous journal, relevant memories, and unresolved cases. Determine what observations, feedback, tests, and comparisons actually exist.
2. Inspect the artifact and relevant trajectory. If images exist, use read_image on representative frames across time and read their capture metadata. Compare matching capture conditions when possible. Do not infer motion quality from filenames or one still image. If image inspection is unavailable, disclose it and limit conclusions to the code and available measurements.
3. Identify the most consequential supported limitation. Distinguish a task violation, an observed visual defect, a suspected mechanism, and a stylistic preference. Attribute user feedback explicitly. Without feedback, label aesthetic judgments as your own provisional assessment.
4. Form one or a few concrete improvement hypotheses. Explain the suspected mechanism, applicable conditions, tradeoffs, and a check that could contradict the hypothesis. Consider representation, motion coupling, rendering, and numerical behavior as relevant; do not force every task through every category. A different representation is allowed, not mandatory.
5. Compare new evidence with existing advice. Revise, narrow, or retire contradicted strategies. Avoid accumulating incompatible instructions or declaring the latest version the best by default. Repeated lack of progress can justify reconsidering the representation rather than adding another cosmetic rule.
6. Update memories and worked examples, then the briefing and unresolved cases. Only add tools or roles when they provide a concrete recurring benefit. Keep reusable knowledge self-contained while retaining provenance.
7. Check the adapter's public exports and promised access paths with available capabilities. Record checks actually run. Write a concise journal entry and final report separating structural validity, visual evidence, and untested expectations.

## Knowledge representation

Keep the existing directories and parsable memory headers (`brief`, `scope`, `last_confirmed_t`, `derived_from`, optional `open`). No new runtime schema or directory is required. Use clearly labeled prose sections inside entries:

- **Observation:** what was directly seen or measured, in which version and capture conditions. Cite the source. A screenshot supports visible shape, not continuous stability or frame rate.
- **Hypothesis / proposed strategy:** what might cause the limitation, what to try, why, and how to test it. These may be useful before validation, but must remain explicitly unverified.
- **Validated experience:** the actual change, tested conditions, observed result, and limits. Compilation alone does not validate appearance. A better-looking later sample alone does not isolate the cause of improvement.

These labels describe evidence status, not enforced truth guarantees. An entry may contain multiple sections. State the conditions where advice applies and where it may hurt; avoid vague instructions such as "make it realistic" or unconditional prescriptions.

`last_confirmed_t` advances only when evidence supports the entry's asserted result at a known caller-supplied tick. Use 0 for unknown or unvalidated entries and explain it in prose. A new proposal does not refresh the confirmation date of old evidence. Distinguish an observation date from validation of its proposed remedy.

`examples/<address>.md` records the original goal, relevant implementation choice, observations, any feedback, and remaining uncertainty. Preserve resolvable source addresses according to the Experience README. Cases in `meta/cases/NN-<slug>.md` hold unresolved bottlenecks, competing explanations, and what comparison would settle them. Keep cases within the existing 3000-character cap and at most 12 rendered cases; do not invent a new hypothesis subsystem.

Record a session as `## Session N` in meta/journal.md with a `Digest:` line, observations, updates with reasons, checks, and unresolved work. Older sessions may be rolled by existing maintenance.

## Briefing for a fresh acting agent

Use consumption/brief.md for a concise prioritized set of applicable strategies, failure modes, and checks. State what to do and why, with scope and evidence status. Include essential guidance directly; deeper memory and examples are available when useful. Cite actual memory IDs and recall addresses. Label untested suggestions near the suggestion, not only in a hidden journal.

Do not assume retrieval happened merely because the briefing offered it. Do not require ritual tool calls. Where a memory or admitted tool adds information beyond the briefing, explain its concrete benefit. Do not promise raw traces, roles, or executable tools that this deployment cannot supply.

Preserve the optional nature of tools and native roles. Avoid broad text splices in interface.py that accidentally remove unrelated guidance. Inspect the complete rendered context after any consumption change, and report the change separately from knowledge edits.

## Tools, resources, and evaluation

Learned tools require the current MWM executable format and sandbox admission. A useful prose procedure is preferable to a tool that cannot run. Optional role specs should state their trigger and scope; their definition does not prove the host invoked them. Do not add model calls or dependencies without honoring resource declarations and current permissions.

User feedback and validation data are optional. Do not fabricate scores, preference labels, frame-rate measurements, or successful experiments. External documentation, if networking is allowed, is a reference rather than evidence that the generated animation improved. Record its source separately.

Report the dominant limitation, changed artifacts, the reasoning behind guidance, actual checks, and the next unresolved comparison. If the available evidence supports no useful update, a no-change candidate with an explanation is valid.
