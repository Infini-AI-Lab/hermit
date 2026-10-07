# OfficeQA Skill

## Retrieval Discipline
- Start by narrowing to the most likely candidate file before reading long passages.
- Prefer targeted search terms that name the exact entity, period, measure, or table concept from the question.
- After a promising match, read only a small surrounding span and verify it matches the requested year, basis, and unit.

- The grep tool matches literal substrings, not regular expressions: alternation (`|`), character classes (`[0-9]`), anchors (`^`, `$`), and escapes like `\d` will return no matches. Search with plain words, table identifiers (e.g. `IFS-1`, `net budget outlays`), or plain digit strings only.
- Parsed files typically strip thousands separators: to locate a value like `1,095,541`, grep the comma-free form (`1095541`) or a distinctive row/table label, then read the surrounding lines to confirm the row and its columns.

- Use returned line numbers to anchor a small read (start a few lines above the match, ~20-70 lines) instead of reading from the top of the file.
- Ground oracle-parsed content in the local files before using its values: grep a distinctive value or row/table label from the oracle page to locate the corresponding evidence line, then confirm with a small read.

## Evidence Discipline
- Extract the exact value from the retrieved text before doing any arithmetic.
- Keep track of each operand's period, unit, and semantic role so nearby proxy values are not mixed in.
- If the question asks for a transformed or derived quantity, compute only after confirming every operand.

- When a row-label grep returns MULTIPLE hits in the same file, the same row label likely appears in several related tables (e.g., cleared-vs-arrived, gross-vs-net, annual-vs-monthly). Before using any hit, read a few lines above each match to identify the table title, and select the hit whose table title and column headers match the question's exact concept and period. Using a sibling table's row with the same label yields silently wrong operands for every downstream computation.
- For chart questions (peak/trough year, value at a date), the decisive figure is usually stated in the narrative prose adjacent to the chart heading, not in a table. Grep the chart title, then read the full paragraph(s) around that heading (up to ~100 lines) looking for sentences naming the peak/trough year or value. Never answer a year or value that no retrieved text explicitly names — an unsupported guess is a wrong answer.
- When a parsed table's period/year header row has been stripped or garbled (columns renamed like `1/.1`, or year groupings collapsed), do not stall re-reading the header. Map the data columns to periods using the oracle-parsed copy of the same table in the prompt (which preserves the layout), then compute and emit. A garbled header is a mapping problem, not a dead end.

- Never substitute outside/world knowledge for values not present in the files or the oracle-parsed pages (e.g., external publication dates, historical exchange rates, macro forecasts). If a required datum cannot be found by grep, re-check the oracle-parsed content provided in the prompt and use the best-supported in-document value; recalled facts are not evidence.

## Final Answer Discipline
- Return the final answer only after one last consistency check against the retrieved evidence.
- Copy the final answer from a checked value, not from an unverified intermediate guess.

## Answer Emission
- Emit only the bare value inside <answer>...</answer>: no sentences, no explanation, no markdown bolding, no currency symbols, no words like "million" or "billion" when the question already fixes the unit, no "approximately", and no thousands-separator commas (write 4170.66, not 4171.41 or $4170.66 million).

- Honor the question's explicit output-shape directives over the bare-value default: if it asks for multiple values "enclosed in square brackets and comma-separated", emit exactly that shape (e.g., [277.76, -39.14]); if it specifies a sign convention, rounding, or unit for each element, apply it to each element.
- Never end a trajectory without a final <answer>. If repeated greps fail, fall back to reading around known table-of-contents line numbers or compute from the oracle-parsed content already provided in the prompt; emit the best-supported answer rather than nothing.

- Act immediately: every turn must issue a tool call or a final answer, never pure deliberation. Long multi-month/quarterly computations (correlations, geometric means, standard deviations, smoothing) should be computed directly from the oracle-parsed table in the prompt or one grounded read — collect all operands, compute step by step, and emit the result instead of stalling on searches for more evidence.
- If repeated reads keep returning the same front-matter/boilerplate content, stop re-reading from the top of the file. Anchor instead on grep line numbers (read 20-70 lines around the match) or fall back to the oracle-parsed table already in the prompt.

## Statistical Conventions
- When a question asks for a "standard deviation" without saying "sample", compute the population standard deviation (divide squared deviations by n); use the sample formula (n−1) only when "sample" is explicitly stated.
- After computing a statistic, re-verify every operand against the retrieved rows (correct row label, correct month/year column, no revised-vs-unrevised mixing) before answering.

## Actual vs. Estimated Rows
- When a table lists both an actual row and an estimated row for the same year (e.g., `2024` vs. `2024 - Est1` in FFO-1), use the actual row for that year, not the estimate. If two bulletins both cover the year, confirm the actual values agree across them before computing.

## Basis-Qualifier Anchoring
- When the question fixes a special basis via a qualifier (e.g., "1941 values should include funds used for urgent ship construction"), grep the qualifier's keywords (e.g., `ship`) to find the footnote or table column that already reports the series on exactly that basis. Use that column's values as-is; do not hand-adjust totals to satisfy the qualifier.
- When the question states include/exclude scope criteria, read the table's footnotes and confirm the chosen column's stated basis matches those criteria before extracting the value.

## Garbled-Row Reconstruction
- If the needed row's value is OCR-corrupted (footnote markers or stray digits fused into the number, e.g. `2/155,598`), do not use the raw token. Reconstruct via internal consistency: (a) sibling columns that echo the same figure (many tables repeat the first month's value in a 'cumulative to date' or 'comparable period' column), or (b) summing the component rows that should total to it. Only compute after the repaired value agrees with at least one independent cross-check.
- If an exact-date grep returns no matches, retry with adjacent date variants (an issue date in the table is often one day off from the question's date, e.g. `12/19/52` vs `12/18/52`), then confirm the row by pairing its maturity date with the issue date before using the amount.

## Revised-Value Flag ('r')
- Distinguish official revisions from OCR corruption when overlapping bulletins disagree: if the later bulletin's overlapping value carries an 'r' (revised) flag next to it, it is an intentional revision — use the revised value for that month even though it differs from the earlier bulletin. Only unflagged, trend-breaking discrepancies (e.g., 303 suddenly becoming 103) indicate dropped leading digits.

## Multi-File and Multi-Bulletin Span Retrieval
- When a question asks for the same measure at the same as-of date (e.g., 'end of June') across several years, iterate the identical row-label grep (e.g., `total assets`) over each year's corresponding bulletin file — one grep plus one small anchored read per file, never a full-file read. Before recording each operand, confirm which column holds the requested as-of date (tables like ESF-1 list two date columns, June 30 first, Sept. 30 second); record the value from the requested date's column only.
- For a multi-year span whose data is spread across several bulletins, grep one distinctive comma-free anchor value from each candidate file to confirm which years it holds, then read only the annual-row block in each file (anchored at the grep line number). Ground each projection endpoint separately in the bulletin whose table reports that year (e.g., the base year from the 'prior fiscal year' columns of a later bulletin), verifying both rows with small anchored reads before computing.

## Growth-Rate Convention
- Match the formula to the requested convention: continuously compounded rate = ln(end/base)/years; CAGR = (end/base)^(1/years) − 1. Do not substitute one for the other.

## Chart Companion Tables
- Chart questions in trust-fund reports often have the charted series backed by companion tables in the same report section: grep the chart identifier first (it appears in the table of contents with a page number), then read the results-of-operations and multi-year projection tables in that same section (e.g., Table TF-15 and its FY projections underlie Chart TF-G). Answer from those tables rather than hunting for the unparsed chart image.

## Side-by-Side Period Tables
- Yield/savings tables often lay out several month-label column groups on one row (e.g., `| 1953-Apr. | 2.96 | 3.24 | 3.23 | 1954-July | 2.45 | ... | 1955-Oct. | 2.82 | ... |`): after each month label, only the following 2–4 numbers belong to THAT month's column group. When collecting a continuous month range from such a layout, first enumerate the requested span (e.g., July 1953–June 1956 = 36 monthly values), read the table span once, assign each value to its month by column-group position — never by absolute column index across rows — and verify the operand count equals the span length before regressing, mediating, or averaging.

<!-- SLOW_UPDATE_START -->
## Cross-Bulletin Reconciliation
- When two bulletins cover overlapping months for the same series, read BOTH tables and reconcile every overlapping month before computing. If one bulletin's value creates an implausible discontinuity (e.g., a series moving smoothly 350, 347, ..., 303 in one file, then suddenly 102, 100 in the other), assume OCR dropped leading digits and use the value that is consistent with the other bulletin's coverage of that same month. Never mix raw values from overlapping bulletins without checking that the overlap agrees.

## No Recalled Facts — Ever
- Never answer from memory of the outside world (publication dates, working-paper numbers, exchange rates, historical figures). If a datum is not stated in the local files or the oracle-parsed pages, derive it from in-document context only: series numbering (e.g., a paper numbered 8703 belongs to the 1987 series listing), adjacent rows, table headers, or the closest reported figure on the requested basis. A recalled 'fact' is a wrong answer, not a shortcut.

## Operand Completeness Before Computing
- Before any multi-period computation, explicitly enumerate the requested span and count the required operands. January 1984 through March 1987 is 39 monthly values — if you have collected 15, you are not done retrieving. Each bulletin covers roughly the 12–13 months ending just before its publication date, so a multi-year span requires data from every bulletin in the source list.
- After collecting, re-verify: operand count equals the span length; every operand comes from the correct row label and period column; units are uniform (millions vs thousands vs dollars).

## Projection Arithmetic
- To project k years forward at the observed compound growth rate, multiply the latest value by the FULL k-period growth factor: latest × (latest/base)^(k/years-in-base-period). Never apply the annualized rate only once. Sanity-check magnitude: a 5-year projection at ~58%/yr must grow ~10x, not ~1.6x; if the projection is not dramatically larger than the base when growth was rapid, you made this mistake.

## Tool Path Hygiene
- Copy file paths EXACTLY from glob/read output. If a grep returns 'path not allowed' or file-not-found, immediately re-derive the path with glob(pattern='*') and copy it verbatim — never reconstruct or concatenate paths from memory. Do not let one bad path consume the whole trajectory.

## Search Time-Box
- After 3–4 failed targeted greps for a datum, stop searching. The oracle-parsed pages in the prompt are the intended source for hard-to-reach data (chart values, exchange-rate series, yield tables): read them, extract the series, compute, and emit. If a required value lies beyond local file coverage (e.g., a December 2013 figure with files through June 2013), search every oracle page and every table that reports multiple as-of dates for the closest figure on the requested basis, use the best-supported value, and emit it.

## Always Emit an Answer
- Never end a trajectory without a final <answer>. The most damaging persistent failures are no-answer trajectories where the search loop ran out of turns. Once you have grounded operands — even partially — compute step by step and emit the best-supported result. An imperfect grounded answer beats no answer.

## Reinforce What Works
- The proven pattern for every stable success: one targeted grep (plain literal strings, comma-free digit forms) → one anchored read of 20–70 lines around the match → verify row label, period column, and unit → compute once with all verified operands → emit the bare value in the question's requested shape. Follow it every time.
<!-- SLOW_UPDATE_END -->
