# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.
- Before extracting anything, write a compact target specification from the question: entity/measure, exact time
  basis and endpoints, requested source or edition, unit/scale, transformation, precision, and output shape.
- Treat the parsed oracle pages and the supplied candidate/source hints as the primary evidence. If overlapping
  bulletins or revised tables exist, choose the series required by that evidence and do not silently combine
  observations from a different revision. Label every selected period and column header explicitly.
- For each operand, keep a short ledger containing source and table, row and column, period, value, unit, and
  revision/basis. Do not use a value until its ledger entry agrees with the target specification; this is especially
  important when annual, fiscal, calendar, monthly, quarterly, or year-end rows appear together.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- State the literal formula implied by the question before calculating. Convert every operand to the requested
  common unit before applying it, preserve the requested transformation order, and use the requested population or
  sample definition and rounding rule. Use a small executable calculation (prefer `python3`) for multi-step work,
  then check that the intermediate units and magnitude are plausible. Do not round intermediate values unless the
  question explicitly requires it.

### Multi-observation extraction gate
- If the answer uses a range, three or more observations, or a later/revised table, do not build the operand list
  from scattered snippets or from memory. First find one table row or contiguous block that covers the full requested
  span, record its complete column/header path, and copy the raw cells in order.
- Count the copied cells against the question's inclusive period count before calculating. If no single source covers
  the span, make a period-by-period ledger, compare overlapping values from candidate sources, and use only the
  series whose headers, periods, revision basis, and source role all match the target. Do not silently splice a
  plausible value from another edition. This gate ends after one such reconciliation pass.

### Cell-integrity gate
- When a cell contains a footnote marker, revision letter, asterisk, standalone dash, slash, or adjacent text, separate
  that annotation from the numeric token before recording the value; preserve a leading minus sign as part of a
  negative number. Re-read the row and column headers and check that the resulting scale and sign are plausible
  relative to neighboring cells; never treat an annotation's digits as part of the value. Resolve an anomalous cell
  from the same table or an explicitly matching authoritative table before using it.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from the checked value, not from an unverified intermediate guess.
- The content between `<answer>` and `</answer>` must be only the requested value or requested ordered structure:
  no explanation, headings, markdown, citations, currency symbols, unit names, approximation words, or trailing
  punctuation unless the question explicitly requires them. Re-read the target specification for the exact scale,
  precision, sign, order, and count immediately before emitting the wrapper, then stop.
