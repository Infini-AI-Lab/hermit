# OfficeQA Skill

## Required workflow
For every question, use this order and keep the checks bounded:

1. Parse the requested measure, reference period, time basis (calendar, fiscal, monthly, quarterly, or annual),
   source/publication timing, units, and requested precision or output shape.
2. Locate the authoritative table or passage. Before copying any number, make a small mapping of
   `requested period -> source edition/table section -> row label -> column/header -> value`. If a table has
   continued pages, repeated year blocks, cumulative/comparable columns, revised series, or overlapping editions,
   map the block and date label explicitly; do not infer a row or column from its physical proximity. Use the
   contemporaneous source when the question asks for observations over time, and use later/revised data only when
   the question explicitly calls for it. Resolve an ambiguity with at most one targeted alternate-source check.
3. For a derived answer, write the formula in words or symbols and label every operand with its mapped period,
   measure, unit, and sign. Check the operand count/order and definition (for example, percent versus percentage
   points, population versus sample, or level versus change) against the question. Compute using a tool when
   practical, without premature rounding, then re-read the mapped source values once before the final computation.
   If the source series cannot be verified, do not replace it with a nearby claim or guessed observation.
4. Before sending the final response, perform one output gate: reread the question's requested answer form,
   precision, scale, sign, and number/order of values. Put only the requested value(s) inside `<answer>...</answer>`:
   no explanation, calculation, currency symbol, commas, or unit words unless the question explicitly requires them
   as part of the answer form. This gate applies even when the reasoning above contains useful explanation.

## Semantic lock for ambiguous tasks
- Before extracting or calculating, write a one-line literal contract: the exact population/period, operation,
  transformation, and output requested by the question. Treat source labels such as fiscal year, calendar month,
  current/prior period, annualized rate, cumulative total, and revised series as binding metadata.
- If two interpretations are plausible, name both briefly and reject the one that conflicts with the question's
  wording, table headers, units, or requested output. Do not silently use a familiar default (such as population
  instead of sample standard deviation, a month-to-month difference instead of an aggregate ratio, or a raw
  annualized rate as a quarterly rate). Use one coherent source series unless the question explicitly requests a
  splice or revision comparison. This is a single bounded decision, not an invitation to search indefinitely.
- After selecting the interpretation, perform one independent sanity check: restate what each operand represents and
  verify that the formula's dimensions and endpoint/window count match the contract. Correct the operands or formula
  before rounding if they do not.

## Primary-page precedence and conflict ledger
- Read the task's `Oracle Parsed Pages` before broad corpus searching. Treat the page's table title, column hierarchy,
  row labels, date basis, and values as the primary evidence for the requested observations. Candidate files and later
  editions are navigation or corroboration, not permission to replace a primary-page observation.
- When several observations are required, make a compact ledger with one row per requested period:
  `period | primary-page heading/row/column | value | source status`. Do not calculate from a value until its
  heading and row/column path match the literal contract. A total must not be replaced by a component, and an
  annual/fiscal row must not be replaced by a month-end row (or vice versa).
- If a primary page omits an observation and a corpus search supplies it, mark that row as corpus-filled and verify its
  exact table title, unit, date basis, and column path. If a corpus candidate conflicts with a primary-page row, do one
  bounded header/footnote comparison and retain the primary-page row unless the question explicitly requests a revision
  or later edition. Recompute from the final ledger, not an earlier hand-copied list.

For standard numeric operations, you may use `python3 scripts/stats.py` after the semantic lock. Supply only the
explicit, already-verified values and select the matching operation; this helper does not decide what the question
means or which source values to use. It supports `mean`, `geomean`, `sd` (add `--sample` when required),
`percentile --q Q` using linear interpolation on a 0-to-1 quantile, and `trimmed_mean --trim F` where F is the
fraction removed from each end. Recheck the operation, count, and units once after it prints.

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
