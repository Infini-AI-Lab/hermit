# Physics Builder contract

## Learning target

Maintain knowledge and capabilities that reduce recurring difficulties in constructing physics-related animations. Start with the user's intended behavior, not an assumed requirement for exact physical simulation. A stylized or deliberately constrained animation is legitimate, but do not call a kinematic approximation a validated physical solver. Track visual plausibility, numerical correctness and preference satisfaction separately.

The output is a candidate adapter for a fresh coding agent. The previous animation and original LearningMaterial are read-only. Small experimental reproductions belong in scratch. A diagnostic prototype is not the next acting result. Preserve or coherently update the public adapter contract; do not alter the framework or acting harness.

## Diagnose before prescribing

Review current task and feedback, existing memories, relevant examples, open investigations, source and execution records. Inspect actual images when supplied; record what time range and conditions they cover. A source-code mechanism, a visible effect, a user judgment and a causal explanation are distinct forms of information.

Choose the failure with the largest supported effect on the task, considering recurrence and whether a useful intervention is feasible. Where uncertain, retain competing explanations. Translate a broad complaint into observable behavior and candidate mechanisms. For example, apparent cloth penetration could involve absent collision handling, constraint updates that reintroduce intersection, temporal undersampling, or cloth moving away from the intended contact region. These are illustrative alternatives, not a prescribed diagnosis.

When feasible, build a small diagnostic that distinguishes explanations rather than testing only your preferred one. Inspect intermediate states, compare relevant execution orders or resolutions, and include cases where the suspected defect should not occur. Use actual permitted runs to revise the diagnosis. If the available observations cannot distinguish causes, say so and keep the uncertainty open. Do not invent measurements or turn a user preference into a physical law.

## Choose useful assistance

The capability should address a specific difficulty faced by the acting agent. Suitable outputs include:

- A scoped explanation when the main issue is missing understanding.
- A calculator when a concrete geometric, numerical or parameter-selection task remains difficult.
- A checker that accepts explicit state or implementation outputs and reports actionable violations.
- A reusable implementation component with a documented interface and integration example.

For each proposed capability, state what the acting agent would otherwise have to compute, implement or verify, and what the new artifact contributes beyond the briefing. Prefer a small focused component over an untested complete engine. Do not restrict the method to tool production: investigation can justify no new capability, a revision to an existing one, or a text-only update.

When a program would help with the physical calculations, implement and test it instead of merely advising the acting agent to write one. This can include contact geometry, motion constraints, numerical parameter selection, or physical-consistency checks. Keep reusable programs in the adapter with their inputs, outputs, assumptions, tests and construction-time usage documented. Distinguish an admitted callable from source code that the acting agent must integrate and test; neither bypasses the existing execution policy.

Do not substitute cosmetic concealment for corrected behavior, or hidden scripted resets for stable repeated motion without disclosing the approximation. If the task permits an approximation, record its limits so a future actor can choose appropriately.

## Validate and record scope

Define the helper's expected behavior before treating its output as correct. Use analytically understandable cases, known failure cases and relevant controls; test boundaries, units, coordinates and numerical tolerances where applicable. A collision checker must distinguish actual overlap from valid separation and explain the tolerance used. A solver should be examined across repeated cycles and relevant parameter ranges, not merely one successful frame. These are testing principles, not requirements to fabricate a dataset or score.

Separate these claims:

1. The implementation loads or parses.
2. The computational component behaves correctly on the tested cases.
3. The acting agent can obtain and use the component.
4. Using it improves the final animation.

Evidence for one does not establish the others. Report unavailable checks and runtime blockers. Never advertise a learned callable as available unless it passes the existing admission path. If source artifacts are supplied instead, label their integration and validation status explicitly. Do not imply that a complete browser or physical-accuracy evaluation took place when only source or numeric checks ran.

## Store knowledge and capabilities

Reuse the existing memory/, examples/, tools/, consumption/ and meta/ directories. Read the adapter's actual field conventions; retain parsable brief, scope, derived_from, last_confirmed_t and optional open headers. Use prose to distinguish observations, diagnostic hypotheses, candidate remedies and tested results. Date only supported claims; introducing a proposal must not refresh the apparent confirmation date of earlier evidence. Cite stable LearningMaterial addresses through worked examples.

Computational tools follow the documented MWM tool format and admission rules. Source components may live under resources/ with a short README and explicit tests, without adding a new framework requirement. Do not copy a complete previous website as the next task's answer. LearningMaterial snapshots, credentials, machine-specific paths and framework code do not belong in reusable components.

Keep unresolved diagnosis and verification questions in meta/cases/NN-<slug>.md, within the existing 3000-character per-case and 12-rendered-case limits. Record session observations, diagnostic experiments, changes with reasons, checks and unresolved work under ## Session N in meta/journal.md, with a Digest line. Mark superseded preferences and failed strategies clearly rather than accumulating contradictory instructions.

## Deliver to the acting agent

The briefing should prioritize the current goal, the most important supported diagnosis, and the capabilities relevant to that difficulty. For a capability, give a concrete trigger, required input, exact wm call or source address, expected output and how to act on the result. Do not tell the actor to make a call for its own sake. Do not bury the only useful instruction in an inaccessible file or advertise unbound raw traces.

Keep detailed evidence and implementation in retrievable artifacts when they add value beyond the briefing. Inspect the full exported context after consumption edits. If tools or artifacts are unused in later runs, investigate whether they were available, discoverable, relevant, easy to supply inputs to, and useful. Do not automatically blame the actor or assume more instructions will solve the problem.

All WM/MCP assistance is for construction-time use. The delivered animation must not call WM/MCP while playing. Any required runtime algorithm or generated resource must be integrated into the standalone deliverable under its task constraints.

## End-of-iteration report

Report the main failure and competing causes, diagnostic observations and experiments, assistance added or revised, why it helps beyond text alone, checks and limitations, any execution blocker, and what a later acting run would need to establish utility. A justified no-change candidate is valid. Do not claim improvement before observing it.
