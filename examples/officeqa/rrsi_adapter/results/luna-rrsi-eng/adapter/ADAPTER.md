# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- If retrieval exposes two or more plausible rows, columns, periods, series, or source vintages, lock the
  candidate before calculating: write a compact comparison of each candidate's period, basis (calendar/fiscal or
  other stated window), series/row, column meaning, unit, and source vintage; eliminate every candidate that does
  not match the question's wording. Use the surviving candidate only. If more than one survives, search one more
  targeted source span for the distinguishing header or footnote; do not silently choose by position or familiarity.

## Final Answer Discipline
- Before answering, reread the question's requested output: identify the required value(s), order, sign, scale,
  precision, and any form it explicitly requires. Do one bounded final check that the selected value(s) and those
  attributes agree with the checked source/calculation.
- Then emit exactly the requested content inside `<answer>...</answer>`: a bare value or ordered list of values,
  with no explanatory sentence, label, derivation, Markdown, quotation marks, or decorative markup. Do not add a
  currency symbol or unit unless the question explicitly requires that symbol or unit as part of the answer form.
  Preserve required minus signs, decimal places, percent signs, and ordering; do not invent a cleaner substitute.
- Copy the final answer from the checked value, not from an unverified intermediate guess. Once this serialization
  check passes, stop and send the answer; do not append a justification after the tags.
