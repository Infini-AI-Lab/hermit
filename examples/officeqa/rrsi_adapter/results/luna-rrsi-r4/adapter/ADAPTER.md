# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- Before calculating, make a compact operand ledger: requested entity/measure, source table or definition, period
  convention (calendar, fiscal, month, quarter, or endpoint), exact row and column, unit, and every observation
  needed. Compare that ledger with the table headers and notes. For a series, count the observations and, when an
  annual or subtotal check is available, reconcile the extracted values to it; if it fails, re-open the source and
  correct the extraction before calculating. Do this verification once, then proceed.
- Write the formula or comparison rule in words and symbols before using a calculator. Ensure it matches the
  question's statistic (for example, the requested population/sample convention, endpoint, tail, or percentage
  basis), and do not substitute a nearby measure merely because its label looks similar.
- For a derived or multi-field result, make a small definition map immediately before calculating: one row per
  requested field with its meaning, exact formula, operands, units, and convention. Treat the question's named
  convention as binding: do not add an unrequested normalization (such as dividing a change by elapsed time),
  replace a raw change with a percentage change, or substitute a related risk/statistical definition. Show unit
  conversion as an equality with both source and target units (including scale factors), and check that the
  resulting magnitude and sign are plausible. If a definition is ambiguous, resolve it from the question's
  wording and source notes before calculating; do not silently choose the most familiar convention.
- If the requested result depends on counting, ranking, or locating features in a chart and the parsed passage has
  no plotted observations, do one bounded recovery pass: search the local corpus and the listed source files for an
  alternate table, caption, data series, or page asset that explicitly represents those observations, then verify
  its title, period, units, and series before using it. Do not infer an exact chart result from a narrative trend,
  an axis impression, or an unsupported guess; if the recovery pass finds no explicit representation, preserve the
  strongest directly evidenced result rather than fabricating a precise value.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
- Treat the question's requested answer structure as a strict schema. Before sending, check the number and order of
  fields, requested rounding, sign, scale, and whether brackets or separators are required. Inside `<answer>`, emit
  only the requested values and the literal structure the question specifies: remove currency symbols, percent
  signs, digit-grouping commas, unit words, explanatory prose, and terminal punctuation unless the question
  explicitly requires that item. Preserve requested nonnumeric labels (such as a category) only when they are a
  field in the requested structure. Perform this as the final step and do not add a sentence around the answer.
