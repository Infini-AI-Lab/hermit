# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- When the task includes pre-parsed source pages, treat their table headers, row labels, notes, and values as the
  authoritative starting snapshot. Use the corpus to fill a genuinely missing observation or resolve an ambiguity,
  but do not silently replace a snapshot value with a result from another issue, revision, period basis, or column.
  If a search returns a competing observation, record which exact header path and time basis wins before using it.
- If any later search changes a source, period, header, footnote interpretation, or operand, invalidate the old
  operand and recompute the entire derived result from the current snapshot; never patch only the displayed final
  number or retain earlier intermediate values. Activate this replacement rule only when a correction or competing
  observation actually appears, so ordinary single-source questions remain quick.
- When more than one plausible row, period basis, edition, or series appears, pause before calculating and make a
  short operand ledger: for every input record the requested measure, time basis, period, unit, table context, and
  source location. Reject candidates that do not match the question on every field, and reconcile any remaining
  conflict by reopening the surrounding headers or a corroborating source. Do not choose a plausible candidate just
  because it is numerically convenient.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
- Before sending the final message, reread the question's requested answer type, precision, ordering, and units. The
  contents of `<answer>...</answer>` must be exactly the requested result and nothing else: no explanation, sentence,
  labels, Markdown, currency symbols, or extra unit words unless the question explicitly requires them. Keep any
  reasoning outside the tag, and perform this serialization check after all calculations are complete.

## Calculation Convention Gate
- When the question names a calculation convention (for example geometric versus arithmetic mean, population versus
  sample dispersion, equal-end trimming, a return versus a level change, or midpoint/arc change), write the exact
  definition and denominator or retained observations before calculating. Treat the question's modifier as binding;
  do not substitute a familiar default.
- For a statistic with more than one plausible convention, list the competing formulas in one short line, select the
  one whose words match the question, and do one bounded sanity check: confirm the observation count, endpoints,
  divisor, trim fraction, or interval used. Do not report an alternative merely because it is easy to compute.
- Compute only after that gate. Use `python3` or `awk` if available; if a calculation needs a repeatable aggregate
  over a numeric list, you may instead run `bash hermit/rrsi-adapter/scripts/stat_calc.sh MODE values...` and inspect
  its one-line result. The helper is arithmetic only: source interpretation and formula selection remain yours.

## Multi-stage Transformation Lock
- Activate this only when the question combines source observations with a change, rate, conversion, forecast,
  return, tail statistic, or other derived quantity. Keep a separate raw-observation list and derived-value list;
  label the units and formula for each stage, and write the requested order of operations before calculating.
- Each stage must consume the operands specified by its definition. Never feed a converted, differenced, annualized,
  or forecast value back as though it were the original source observation, and never apply one shared conversion to
  observations whose question-defined factors differ. For a reference distribution, identify its exact members and
  population/sample convention from the question before computing the target statistic.
- Before answering, compare the code or numeric list with that staged record: verify the number and order of raw
  observations, the number of derived observations, and the divisor or tail cutoff. If a stage is ambiguous, resolve
  it from the question and source headers before running arithmetic; do not select the result that merely looks
  plausible.
