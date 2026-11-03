# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.
- Before calculating any nontrivial result, make a compact specification from the question: the requested
  measure (not a nearby aggregate), the exact row/column or series, the complete period/window, the source
  vintage or table to use, the units, and the operation/convention (direction, sign, population vs. sample,
  arithmetic vs. geometric, percentile, and rounding). Check each extracted operand against that specification;
  if a candidate conflicts, stop and resolve the conflict from the authoritative table before calculating.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.
- Treat the final response as a serialization step: reread the question's requested format, then put only the
  requested value or requested structured values inside `<answer>...</answer>`—no explanation, labels, equations,
  bolding, markdown, intermediate work, or trailing punctuation. Preserve the requested order, brackets,
  separators, sign, units/symbols only when the question explicitly calls for them, and precision; put any
  reasoning before the tag and end with the tag. If the question does not specify a special structure, use the
  shortest bare representation of the checked result.

## Evidence Go/No-Go Gate
- Before any nontrivial calculation or derived answer, make a short operand ledger (in your working notes) with one
  row per operand: requested meaning, exact reporting-period label, source table/chart heading, row and column labels,
  raw cell value, and unit. For a series, record the first and last labels and the count, not just a pasted list.
- Do not mark an operand as checked from a search hit or a manually transcribed list alone. Re-open one contiguous
  excerpt that contains the table/chart heading, relevant headers, and the selected row or observations, and compare
  every ledger field to that excerpt. If the excerpt cannot show the relevant label and cell, the operand is unresolved.
- If sources disagree, a period label is fiscal versus calendar, a vintage is revised versus original, or a search
  failed and a nearby value is tempting, stop the calculation. Run at most one additional targeted corpus search for
  the exact measure plus period; then choose only the source whose labels and units satisfy the question, explicitly
  discard the conflicting candidate, and update the ledger. Never repair a missing cell by guessing or silently
  substituting a neighboring row, month, year, frequency, or table.
- Proceed only when every operand has passed the contiguous-excerpt check and all conflicts are resolved. Then compute
  from the ledger, and do one independent formula/convention check before the final serialization. This gate is a
  bounded stop-and-resolve step; it does not require exhaustive searching.

## Missing-Representation Recovery
- Trigger this bounded recovery branch only when the requested table, chart data, image, or publication metadata is
  absent from the text you found, or when an archive utility used to inspect a candidate source fails.
- First check for `python3`. If it exists, use its standard library (`zipfile`, `tarfile`, and ordinary text/image
  file operations) to inspect the local archive members and extract the member whose headings or dates match the
  request; do not stop after merely listing member names. Read the extracted text or inspect the extracted artifact
  before recording any operand.
- Make at most one such archive/artifact recovery pass. If it finds no matching local representation, mark that
  operand unresolved and do not replace it with a nearby narrative statement, unrelated table, or guessed chart
  value; retain the normal evidence gate and answer only from supported evidence.

## Literal Derivation Stop
- On the same derived-answer trigger, do not proceed directly from an extracted list to a number. First parse the
  question into `inputs -> operation -> output` in transient working notes, including the scope and order of any interval,
  trimming, weighting, rate, or statistical convention. Compute from that written mapping (prefer a short Python
  calculation for nontrivial arithmetic), then compare the result with a second evaluation of the same written
  formula. If the two evaluations differ, or the mapping leaves a convention unresolved, perform one targeted
  reread of the authoritative excerpt and resolve it before serialization; otherwise stop after that one check.
