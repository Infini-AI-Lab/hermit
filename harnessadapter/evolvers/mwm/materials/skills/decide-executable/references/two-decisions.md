# Two worked decisions, as they should appear in the journal

## Decision A — built a tool

Evidence: three episodes this block hand-verified an output format field by
field; two slipped on ordering and burned turns re-checking.
Litmus: the check is input -> pass/fail with an exact rule set; scriptable.
Decision: wrote tools/format-check (test.py passes; advertised with a
trigger line). Journal entry: "format verification recurred 3x, hand-checks
slipped twice; exact spec -> tool. First record expected next block."

## Decision B — declined to build

Evidence: two episodes configured the same subsystem differently and both
passed; the "right" configuration depends on the environment's state at the time.
Litmus: what "correct" means moves — the contract itself drifts.
Decision: kept prose (a dated memory entry), no tool. Journal entry: "the rule
IS the moving part; a program would freeze one state's answer. Prose,
scoped to the observed range."
