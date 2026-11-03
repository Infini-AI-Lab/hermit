# OfficeQA Skill

## Semantic Check Skill
- For any derived, statistical, interval, forecast, or extremum request, read
  `hermit/rrsi-adapter/skills/semantic_check/SKILL.md` before calculating and follow its bounded interpretation
  card. This is required even when the operand ledger passes validation: a complete ledger does not establish that
  the selected operation or convention is the one the question asks for.

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

### Source arbitration gate
- Treat the task's pre-parsed source passage and named candidate source context as the anchor. A later or
  revised edition is supplementary, not a replacement, unless the question explicitly requests that later
  edition or the anchored source cannot contain the requested period.
- Before combining editions or tables, write a one-line coverage map: which requested periods each source
  supplies, and whether a value is original, revised, annual, monthly, fiscal, or calendar. If two sources
  cover the same requested period, stop and resolve the conflict from the requested source context and headers;
  never choose the value that merely makes the calculation convenient.
- Do not declare a series complete until its row label, column header, period basis, endpoint convention, and
  unit are visible for every operand. If any requested component is absent, keep searching the corpus or report
  the uncertainty in reasoning; do not fill it with a proxy or guess.

## Final Answer Discipline
- Before answering, reread the question's requested answer shape: scalar versus list, component count, order, sign,
  precision, and any explicitly requested unit or label. Put only that shape inside `<answer>...</answer>`: no
  explanation, Markdown, currency symbol, thousands separators, unit words, or surrounding sentence unless the
  question explicitly requires that item. Do not let the prose of the reasoning leak into the answer.
- Perform one bounded final check against the retrieved evidence and the question, then answer. Copy the result from
  the checked value, not from an unverified intermediate guess; never keep searching after this check is complete.

## Source-Locked Extraction
- When a question needs one or more table values, first write a compact ledger in your reasoning for every operand:
  source document and table, row label and column header, period/endpoint, displayed scale, and exact transcription.
  Confirm that all operands belong to the requested series and table context before calculating.
- If a search exposes multiple plausible rows, editions, endpoints, or series, reject any candidate whose header,
  period, endpoint convention, or unit does not match the question. Do not substitute a nearby value merely because
  it makes the calculation convenient. For a multi-value extraction, count the requested observations and verify the
  first and last against the ledger before proceeding.
- For a multi-value extraction, use the bounded audit helper once after transcription: pipe one tab-separated
  record per operand to `python3 hermit/rrsi-adapter/tools/audit_ledger.py --expected-count N`, where each record
  is `period<TAB>value<TAB>source<TAB>row<TAB>column<TAB>unit`. Replace N with the count required by the
  question. Fix any reported duplicate, missing-field, nonnumeric-value, or count error before calculating.

## Calculation and Units
- Translate every derived request into an explicit formula before evaluating it, including the comparison scope,
  endpoint inclusion, statistic convention, and requested rounding. Use all and only the observations in the ledger.
- Preserve each operand's displayed scale independently; convert each one to a common unit before arithmetic, and
  retain full precision until the final requested rounding. If a convention is ambiguous, resolve it from the
  question's wording and source context rather than silently trying an unsupported alternative.
