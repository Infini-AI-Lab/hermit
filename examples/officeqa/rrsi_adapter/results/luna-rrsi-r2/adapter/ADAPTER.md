# OfficeQA Skill

## Robust local inspection and arithmetic fallback
- When a search identifies a relevant line in a corpus file, use `scripts/show_context.sh FILE LINE [RADIUS]` for the next inspection pass. Use a small radius, and treat its numbered output as the evidence span: re-check the full row, all relevant headers, and the period before copying an operand. This avoids hand-written line ranges and invalid ranges; it does not replace comparison of the table's hierarchy.
- If a preferred calculation runtime is unavailable, use `scripts/calc_fallback.sh` rather than hand-entering a substitute. It supports `geom VALUES...`, `mean VALUES...`, `sd_sample VALUES...`, `sd_population VALUES...`, and `smooth ALPHA VALUES...`; verify that the mode and observations match the question, and independently sanity-check the result. These helpers are bounded local arithmetic only and cannot supply missing source evidence.

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Cell and Series Verification
- Before extracting any operand, write down the requested measure, population/scope, period, row label, column label, and unit. Confirm each against the table's own headers and nearby hierarchy; do not substitute a subtotal, total, similarly named series, or adjacent period merely because its value is plausible.
- If the answer depends on choosing a minimum, maximum, crossover, or other extremum, evaluate the stated criterion over every candidate observation in the requested range before looking up the final cell. Recheck the winning observation's row and column headers.
- If overlapping bulletin versions disagree, pause the calculation and reconcile the observations: compare the same period and cell across versions, use an explicitly marked revision or the authoritative/latest presentation when supported by the source, and keep one reconciled series. Never silently mix transcriptions or retain an earlier result after correcting an operand.

### Period-to-column gate
- For any table with multiple periods, repeated month names, grouped/merged headers, continuation pages, or more than one rendition, make a small period map before copying values: list each requested period in order beside its exact row/column header path and value. The map must identify the year (or other higher-level header) that governs every repeated subperiod.
- Do not infer a flattened column alignment from position alone. If the extracted text has lost a merged header or the map has an unassigned, duplicated, or out-of-range period, make one bounded search for an explicit header anchor or a clearer rendition and rebuild the map; do not calculate until every operand has a mapped period. For a calendar-year or month-end change, use the rows matching that calendar convention, not a nearby annual or midyear row.
- For a long series, count the mapped observations and check its first and last periods plus gaps/duplicates against the requested interval. Re-open only the source span for any mismatch, then use the reconciled map as the sole input list.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

## Calculation Discipline
- Translate the question's wording into a written formula before calculating, including the requested definition, interval, denominator, sample/population convention, sign, scale, and rounding rule. If two conventions are plausible, resolve the choice from the question or source definition rather than silently choosing one.
- Use a local calculation tool when arithmetic is nontrivial, retain guard digits until the final requested rounding, and independently recompute or sanity-check the result once.

### Dimensional ledger
- Before running the formula, write every operand as the literal source number plus its unit/scale (for example, a displayed suffix or a percent-versus-fraction meaning), then write one explicit conversion to a common calculation unit. Apply that same conversion to every operand of the operation; never remove a suffix from only one value or mix displayed and converted scales.
- After calculating, inspect the result's unit and order of magnitude against the source values and formula. If the result implies an implausible scale, sign, or rate convention, re-read the wording and the ledger and recompute once before rounding. Keep the requested output scale separate from the internal calculation unit.

### Result-identity gate
- Trigger this gate when the wording asks for an endpoint, change, rate, average, extremum, forecast, projection, smoothing, elasticity, growth, or a named statistic, or when a table offers annual, monthly, cumulative, subtotal, and total alternatives.
- Before copying the final operands, write a one-line result identity containing: the exact quantity being returned, the requested period basis and endpoints, the source series/row, the operation, and the output unit. Then list the nearest plausible alternative (for example, an annual row versus a period endpoint, an intermediate statistic versus the requested forecast, or a component versus a total) and state why it is excluded using the question or table heading.
- For an extremum or comparison, do not select a candidate from a promising search hit: enumerate every candidate in the requested range and compare the stated criterion. For a forecast or rate, distinguish the observed interval from the projection interval and apply the requested transformation in that order. For a named statistic, use the statistic's stated convention and do not submit a nearby statistic merely because it is also available.
- Do not calculate or answer until the result identity, operand roles, period basis, and exclusion of the nearest alternative are explicit. If the identity is still ambiguous after one targeted source check, use the wording and authoritative table heading that most directly define it; never silently switch interpretations after calculating.

## Sourced-Input Gate
- Every manually supplied input must have a source span. If an input is missing or conflicting, make at most one additional targeted search of the local corpus and one comparison against the best available primary presentation. If it remains unsupported, do not calculate from it or invent a replacement; use the strongest supported evidence and answer without claiming the unresolved input is verified.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
- Treat the requested output shape as a hard constraint. Immediately before answering, reread the question and determine whether it requires a scalar or ordered multi-value structure, the scale, precision, sign, and any explicitly requested unit. Put only that payload inside `<answer>...</answer>`: no explanation, labels, units, currency symbols, percent signs, thousands separators, equations, Markdown, or surrounding prose unless the question explicitly requires them. For multiple values, preserve the requested order and use only the minimal structure needed to distinguish them.
