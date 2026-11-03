# Pattern Library (reference, not an allowlist)

<!-- Adapted from google-research/rrsi domains/coding/PATTERNS.md and domains/workspace/PATTERNS.md (Apache-2.0,
     commit be50316) for an adapter of an agent that answers questions from a document corpus. -->

Known mechanism families for document-question agents, with concrete examples and generic engineering traps. You
may adopt, adapt, ignore, or invent formats not listed here.

## 1. Answer-form mechanisms

Exact-match scoring makes the answer's form part of the answer: the requested unit and scale, rounding and
precision, sign, the order and count of several values, a bare value versus a sentence.
Mechanisms: a pre-answer checklist that re-reads the question's own wording for unit, scale, precision, order and
count; a rule that the content of `<answer>` is the value alone in the form the question asks for.

Generic trap: a rule tuned to one question's quirk (one specific rounding, one unit) hurts every other question;
derive the form from the question's words, never from a fixed choice.

## 2. Evidence procedures (finding the right operand)

The agent loses most points by reading a neighbouring row, column, edition or period, or by mixing a revised and
an original figure. Mechanisms: an ordered procedure (identify the measure, the period and the source the question
names; find the table; confirm the row and column headers before taking a value); a helper that prints a table's
rows matching a label with their headers; recording each operand with where it came from before computing.

Generic trap: unbounded searching across the corpus; every search procedure needs a stopping rule.

## 3. Computation mechanisms

Multi-step arithmetic (sums, differences, growth rates, standard deviations, averages) done in the head drifts.
Mechanisms: compute in Python with the operands written out; state the formula the question implies (population
versus sample, percent versus percentage points) before computing.

Generic trap: a fixed formula choice that matches some questions and contradicts others; let the question decide.

## 4. Workflow mechanisms (no persistence)

Examples: a single verification pass before answering ("re-open the source line of each operand once"); a
scratch file of found operands the agent re-reads before answering; a loop breaker when the same search repeats.

Generic trap: "check again" without an exit; every verification needs a max-fires bound and must never delay the
final answer past the time limit.

## 5. Context-budget mechanisms

Long table dumps flood the context. Examples: helpers that print only the matching rows; reading a page range
rather than whole files; keeping the operand list short and explicit.

Generic trap: printing whole files to "be safe".
