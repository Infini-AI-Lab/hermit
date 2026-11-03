# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Before searching deeply, write a compact target specification from the question: requested output (level, endpoint,
  difference, ratio, ranking, or other operation), exact time basis and endpoints, series/measure, required row and
  column, revision or consolidation status, unit/scale, and requested precision/order. Treat every item as a
  constraint, not as a hint.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches every target-spec constraint.
- When multiple plausible rows, columns, periods, or editions appear, make a short candidate comparison and reject
  each mismatch explicitly. For repeated monthly or annual blocks, reconstruct the year/period labels before selecting
  a row; do not infer them from search order or nearby prose. Treat a later bulletin as authoritative only when the
  question explicitly asks for the latest/revised/consolidated presentation or the source explicitly marks the earlier
  observation as superseded. Otherwise prefer the table whose title, reporting basis, and publication context match the
  requested series. Never splice values from competing blocks merely because their row labels look identical: select
  one complete table block, or record why a deliberate source transition is required.
- Before calculating, re-read the target specification and record each operand with its period, source series,
  revision status, unit, and semantic role. If the requested series spans blocks, verify the boundary labels and the
  table identity at each transition; a later block may corroborate a value but cannot silently replace the selected
  block. Continue searching for a later consolidated/revised series only under the explicit conditions above.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, state the formula implied by the question and confirm
  every operand before computing. Resolve whether a statistic is population or sample, whether a rate is a percent or
  percentage-point difference, and whether a requested quantity is a level or a change from the wording and source
  context; never substitute a familiar convention. Convert all operands to one common unit before arithmetic, and
  use a tool such as `python3` or `awk` for multi-step calculations while retaining enough precision for the requested
  rounding. Write the operation order explicitly before running the calculation: for an interval aggregate, combine
  the requested components over the full interval before taking a ratio or percentage; for a change, identify the
  specified endpoints; for a rate or index, apply the stated period and denominator convention. Then check that the
  equation's dimensions and rough magnitude match the requested result (including scale, sign, bounds, and the number
  of observations). If an alternative convention remains plausible, calculate or compare it only as a bounded check and
  choose the one supported by the question's nouns and table definitions.

- For a long repeated series (more than 12 operands), or whenever values are entered manually from several table
  blocks, treat transcription as an unverified intermediate. After assembling the list, re-open the source rows and
  compare the list against them in order: confirm the first and last period, every block boundary, the expected
  observation count, and each entered value (including signs and missing-value markers). Correct discrepancies before
  calculating; do not rely on a matching count or a plausible aggregate as validation. This is one bounded validation
  pass, not a reason to search indefinitely.

- Resolve explicit temporal and statistical qualifiers literally before using a default convention. For example,
  “calendar year” requires calendar-year endpoints (not fiscal-year or annual-summary endpoints), and “population”
  or “sample” selects that named denominator. For a change, identify the two endpoints and direction in words; for an
  interval statistic, identify the included observations and aggregation order. If the source offers several plausible
  definitions, compare at most two complete interpretations against the question's exact qualifiers and table headings,
  then commit to the supported one before arithmetic.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
- Treat the question's requested answer form as a serialization contract. Before sending, check the requested unit,
  scale, sign, precision, ordering, and number of values. Inside `<answer>...</answer>` put only the requested result
  in that form: no explanation, calculation, labels, Markdown, currency symbols, or extra punctuation unless the
  question explicitly requires them. Preserve a requested unit word when it is part of the requested form, but do not
  append units merely because the source uses them. Keep any reasoning outside the tags only if useful; the tagged
  content itself must be the exact concise answer.
