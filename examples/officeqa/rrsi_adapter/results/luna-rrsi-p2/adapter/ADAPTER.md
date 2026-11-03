# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- If two plausible rows, series, or period labels remain, pause before calculating and reconcile them in one
  bounded pass: write down the requested measure, calendar/fiscal or monthly basis, row and column headers, unit,
  and any footnote markers; then check the chosen record against the table's subtotal/total or adjacent-period
  pattern. Prefer the record whose labels and basis match the question exactly. Do not average or silently choose
  between conflicts, and stop after that one reconciliation pass.

## Final Answer Discipline
- Before answering, reread the question's requested output form and perform this serialization gate: identify the
  requested value or structure, unit/scale, precision, sign, order, and count. Put only that requested value or
  structure inside `<answer>...</answer>`—never an explanation, equation, label, currency symbol, unit word,
  markdown, or trailing sentence unless the question explicitly asks for it. For a numeric result, copy the
  checked digits in the requested scale and precision; for a list or tuple, preserve only the requested delimiters,
  order, and items. This gate applies even when the reasoning above used units or a formula.
- Return the final answer only after one last consistency check against the retrieved evidence and the serialization
  gate. Copy the final answer from a checked value, not from an unverified intermediate guess.
