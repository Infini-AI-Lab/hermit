# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- If the needed value is absent from the current text (especially a chart value or external metadata), read
  `hermit/rrsi-adapter/skills/evidence_recovery/SKILL.md` and follow that one bounded local-recovery pass before
  calculating. Never invent a replacement series, date, rate, or chart reading to make the calculation complete.

  The recovery skill may use `python3 hermit/rrsi-adapter/tools/local_recover.py TERM1 TERM2 --root docs`
  to discover matching files and archive members when archive utilities are unavailable. Use distinctive terms,
  inspect any extracted representation with an available local tool, and treat the helper's output as discovery only;
  it is never evidence by itself.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.

Before sending the final message, perform this output gate after all reasoning is complete:

1. Re-read the task's requested answer form: number of values, ordering, sign, precision, units or scale, and
   any requested words. Do not infer a fixed format from other tasks.
2. Construct the answer payload from only those requested values/components. Remove explanations, labels, dates,
   equations, currency symbols, Markdown, and unsolicited units or words. Preserve a requested unit only when the
   question explicitly requires it as part of the answer representation.
3. Send exactly one `<answer>...</answer>` pair, with no text before or after it. For a multi-part result, retain
   the question's requested brackets/separators and verify the count and order. This gate is a final serialization
   step; it must not change a value that was already checked against the source.
4. Perform a literal round-trip check: mentally copy the payload exactly as it will be sent, compare every space,
   comma, bracket, sign, decimal digit, percent sign, and requested word with the form stated in the task, then
   emit that unchanged checked payload. Never normalize punctuation or omit whitespace merely because the result
   is numerically equivalent.

## Source and Calculation Audit

For any question requiring extraction, comparison, conversion, aggregation, or a derived statistic, make a compact
audit before calculating. First write a source identity line containing the requested publication or reporting
vintage, revision status when specified, frequency, and the exact date basis (calendar, fiscal, monthly, quarterly,
or annual). Do not extract an operand until the candidate table or chart has been checked against that line: confirm
the table title, the complete header hierarchy, the row/series label, and the unit in the same source block. Treat a
similar-looking table from another snapshot as a different candidate, not as a continuation. If candidates conflict,
perform at most one targeted follow-up search for a matching source block; if the conflict remains unresolved, stop
searching and never splice values from candidates merely because their labels look compatible.

For an interval or change, write the two semantic endpoints explicitly before reading numbers. A named calendar
year means the applicable calendar endpoints (normally the relevant month-end rows), and a fiscal-year request means
the fiscal rows; do not substitute an annual summary for endpoint values unless the question asks for that summary.
For a multi-period series, state its first and last period and count the selected observations. Then validate every
operand against its labeled header and period, record one source location and semantic role per operand, and check
the count and endpoints. Convert units before arithmetic and retain the conversion beside the operand.

Do one bounded verification pass immediately before the output gate: check the audit against the task wording and
the source labels, then recompute using only the audited operands. Use the question's wording to choose population
versus sample, level versus change, percentage versus percentage points, denominator, and rounding rule. If a
needed chart value or external fact cannot be verified from available evidence, do not manufacture a replacement
dataset; report the best answer only when the available source supports it.
