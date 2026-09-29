# S2 review — adversarial review of the OfficeQA pack

Reviewer verdict: the pack mostly works as reported. The 21 structural cases, determinism, path safety and the leak-screen port hold up. I found 9 code defects and fixed all of them in S2-owned files. The most serious ones were controller-side performance and finding texts that lost their key values when clipped to 200 bytes. One report figure was also overstated. All S2 tests pass after the fixes, and so does the whole non-slow suite. This report contains no answer values.

Terms used below:
- **Controller / broker**: the trusted process that relays Codex traffic and builds the ledger.
- **Re-derivation**: `observe.py` re-running a pack tool with the model's own arguments, to recover which cells the model saw.
- **Ledger lock**: the `RLock` that `harness_core/ledger.py` holds while it ingests a request. Submits and the relay wait on it.
- **Cold / warm**: first run in a process versus run with the `otables` caches filled.

## 1. Test results

All commands run from `officeqa-pro133-v9-plumbing/` with `PY=../adapter-comparison-work/.venv/bin/python` (Python 3.12, pytest 9).

| command | as delivered | after the review |
|---|---|---|
| `$PY -m pytest tests/test_officeqa_pack.py tests/test_officeqa_tables.py -m "not slow"` | 63 passed, 0.99 s | **69 passed, 1.02 s** |
| `$PY -m pytest tests/test_officeqa_pack.py tests/test_officeqa_tables.py -m slow` | 22 passed, 28.7 s | **22 passed, 28.8 s** |
| `$PY -m pytest tests/ -m "not slow" --ignore=tests/test_v9_codex_live.py` (all streams) | not re-run | **229 passed, 59.6 s** |

- S2 has no live-marked tests. `test_v9_codex_live.py` belongs to another stream and was not run.
- The only warning is the unregistered `slow` marker (see the requests file).

**New regression tests.** There are 6 new tests in `tests/test_officeqa_pack.py`, and one existing CLI test in `tests/test_officeqa_tables.py` gained a new assertion.
- I ran the new pack tests against a scratch copy of the delivered pack code. All 6 fail there and pass on the fixed code:
  - `test_printings_scan_is_capped_and_says_so`
  - `test_range_alignment_is_indexed_and_file_capped`
  - `test_tool_reruns_are_deduplicated_capped_and_split_after_whitespace_collapse`
  - `test_fragment_lines_must_show_numbers_of_that_corpus_line`
  - `test_seed_findings_keep_their_decisive_values_at_worst_case_lengths`
  - `test_period_mismatch_is_silent_on_the_base_period_of_a_change`
- The scratch copy also showed two other failures: a missing `officeqa_tools` module, and the stdlib-imports test tripping on a test file that had been copied into the pack directory. Both come from how that copy was set up, not from the delivered code.

## 2. Defects

Severity reflects the effect on a v9 run. Line numbers are after the fixes.

| # | severity | where | defect | status |
|---|---|---|---|---|
| 1 | **high** (availability) | `otables.py:43-44, 2116-2121, 2168-2170`; `observe.py:49-51, 209, 447` | `printings` with a wide `--years` window, or a label row with no years, analysed up to 1,600 table parts. The controller re-ran it once per call, holding the ledger lock. Measured cold: about 10 s per call, and 32 s for three calls in one command (`printings --table FFO-3 --row 2003-01 --years 1000-9999`, `--table a --row 1995 --years 1939-2025`). Label rows such as `--row Total` hit this even without `--years`, about 7 s. The page also claimed "6135 table parts searched" when at most 1,600 were scanned. | **Fixed.** Scans are capped at 400 parts (`PRINTINGS_SCAN_CAP`). When the cap cuts, the page says "scan capped: the first N of M matching table parts … narrow with --years". An explicit window now starts at most 2 years before a period row. Identical invocations are re-run once, and at most 6 distinct ones per command. Fallback header recovery is capped at 6. After: 2.4–2.9 s cold per wide call, 2.3 s for three identical calls; default period windows are unchanged. |
| 2 | medium (performance) | `observe.py:398-429, 300` | Aligning unnumbered `sed/head/tail/cat` output against the corpus was O(output lines × expected corpus lines), and `cat` accepted any number of files. `cat <40 files> \| grep x` with 400 unmatched lines took 7.25 s. | **Fixed.** A text-to-positions index plus bisect gives the same first-match-after semantics. At most 8 files and 200,000 lines. After: 0.02 s. Located coverage on the v8 none arm is unchanged (943 outputs / 52,647 observations before and after). |
| 3 | medium (the gate's decisive facts lost) | `rules.py:84-100, 218, 312`; `seed_rules.json` | Finding templates put the full column label (up to 80 characters after the engine clips it) ahead of the decisive values. The engine then clips the whole finding at 200 bytes. The delivered E2E finding was already 198 bytes with a 13-character label. About 37% of column labels in the sampled issues are over 40 characters, so the "latest printing … shows X" value and the `alt` cell of `period_match` were cut off. Templates without an offer would then show no alternative at all. | **Fixed.** `short_column()` keeps the last header level plus its printed `(k)`, at most 24 characters; `column_full` is kept in the binding. `FLAG_WHY` is at most 48 characters, and `table.*` names and words are shorter. The 5 templates were rewritten so the decisive values come first. A worst-case render test asserts that every rule keeps its decisive text within 200 bytes. Only the trailing convention clause of `later_printing` and the example list of `row_coverage` can be cut, at extreme lengths. |
| 4 | medium (precision: wrong offer) | `rules.py:64-66, 435-446, 460` | `period_mismatch` fired on the base period of a change question. Example: a Q38-type question about November 1969 year-over-year CPI inflation needs the November 1968 cell. The rule said "the question asks for 1969-11" and offered to substitute the 1969 value, which drives a year-over-year change to 0. The same applies to month-over-month changes and prior-year growth. | **Fixed.** When the question has a change word, a cell exactly one step before a question period (1 or 12 months earlier, or 1 year earlier with the same or an unstated basis) is not a mismatch. FY versus CY of the same year still fires, as does FY versus CY one year apart. |
| 5 | low (grounding integrity) | `observe.py:330-340` | A `file:line:text` or `N:text` fragment was accepted as a corpus line whenever it was a substring of that line. So `245` cut from `1,245`, or a line forged by `echo`/`python`, became a corpus-located observation with a value that line does not contain. | **Fixed.** A fragment must also show only numbers that are numbers of that corpus line. |
| 6 | low (coverage) | `observe.py:159-180` | The ledger passes the command with its whitespace collapsed. So `show_table a 1⏎show_table b 2` failed to parse. Recovery from the page's first line only tries the default, `--stitch` and `--long` variants, so pages filtered with `--match`, `--cols` or `--grep` lost their cells. | **Fixed in the pack** (`invocations()` splits the arguments at a later tool name). The `cd …⏎sed …` case is a request to S4. |
| 7 | low | `observe.py:205-211` | With the new fallback cap, header lines beyond the cap would have become "raw" observations with junk values (line numbers, page counts). | **Fixed.** A page header is never a raw value line. |
| 8 | low (resources) | `rules.py:74-81` | `_tables(ctx)` built a new `Tables`, and so a new sqlite connection, on every call when the pack had no `.tables`, and never closed it. | **Fixed.** One per rule context. |
| 9 | low (robustness) | `cli.py:106-108` | A non-`ToolError` failure, such as `TABLES_INDEX` pointing at a file that is not an index, printed a Python traceback to the actor. | **Fixed.** One line `<tool>: internal error: …` and exit code 3. There is a test. |
| 10 | report accuracy | `S2-pack.md` §2.5 | The v8 coverage replay fed `normalize_output(...).body` to the pack. The real ledger first applies `observable_text`, which drops every physical line that carries a truncation marker; a one-line `read` dict loses everything. Through the real path: 825/2,779 outputs located instead of 943, and 42,147 located observations instead of 52,647 (−20%). | Not an S2 code defect. It is request 1 to S4. |

## 3. Things checked and found sound

- **Determinism across processes.** The actor's CLI process and the broker have different hash seeds. I rendered 4,062 `show_page` pages (default, `--stitch` and `--long`, 11 issues from 1940 to 2020) plus `find_page` and `printings_page` with their cells and attributes, under `PYTHONHASHSEED` 1, 2 and 3. All three gave the same sha256.
- **Paths.** Every corpus read goes through `load_lines`, which uses the basename only and `^[A-Za-z0-9_.-]+\.txt$`. `../../etc/passwd`, `/etc/hosts`, and `filename` keys in printed dicts all fail to escape. The index is opened `mode=ro&immutable=1` and written with mode 0444.
- **FTS and LIKE input.** Input is normalised to `[a-z0-9 ]` and quoted, so `a"b OR c*` and `%_%` are harmless.
- **`ast.literal_eval`.** It runs only on regex-bounded quoted strings, and failures are caught.
- **No leak through re-derivation.** Cells and corpus lines become observations only when their exact output line is present, complete, in what the model saw. A forged page or a fake `show_table` script in the workspace can only produce cells the real tool prints. Rules show new corpus values (later printings, alternative cells) by design; they never show answers. No S2 code or test reads answer keys, graders, network, or credential files; I grepped for them.
- **Leak screen.** It matches the cards screen, except that the question-ordinal pattern is case-insensitive, which is stricter.
- **Contract.** `observations`, `leak_screen`, `seed_text` (1.4 KB) and `tools` all match `contracts.Pack`. The deviations the implementer declared are accurate.

## 4. Remaining risks (not fixed)

1. **Work under the ledger lock** is bounded but still takes seconds: about 2.9 s per wide `printings` re-run, and at most 6 per command. `find_tables the --rows --all` takes about 2.3 s to re-run. Request 3 to S4/S3 asks to compute observations outside the lock.
2. **Tool semantics changed by fix 1.**
   - Very broad `printings` queries now see only the 400 oldest matching parts. The page says so.
   - An explicit window skips issues more than 2 years before a period row, which excludes very early FY estimates.
   - All 21 structural cases and the E2E scenarios are unchanged.
3. **The change guard (fix 4)** also silences a true FY/CY-agnostic mistake when the cell is exactly one year before a question year and the question has a change word ("rate" counts). This lowers recall for those cases only.
4. **Symlinks inside the corpus directory** would be followed by the controller's reads. The prepared corpus is a directory of copied, 0444 files, and re-derivation emits only what the model already saw, so I found no leak path. I did not reject symlinks, because S6 may choose to link corpus files.
5. **Question-spec pins** remain tuned on the development set. Q21 ("expenditures reported in February 1938 and January 1939") is ambiguous and is left unpinned. The parser should be frozen before confirmation, as the implementer noted.
6. **The S2 report's fire-rate and coverage numbers** were measured without the real ledger path (defect 10). Re-measure after S4 decides on request 1.

## 5. Files changed (all S2-owned)

- `officeqa-pro133-v9-plumbing/packs/officeqa/otables.py`: printings scan cap, early-year clamp, honest scan count.
- `officeqa-pro133-v9-plumbing/packs/officeqa/observe.py`: re-run caps, deduplication, `invocations()`, indexed alignment, numeric fragment check, header handling.
- `officeqa-pro133-v9-plumbing/packs/officeqa/rules.py`: `short_column`, shorter binding fields, the change guard with `step_before`, cached fallback tables.
- `officeqa-pro133-v9-plumbing/packs/officeqa/cli.py`: one-line internal errors.
- `officeqa-pro133-v9-plumbing/packs/officeqa/seed_rules.json`: the 5 templates, rewritten so the decisive values come first.
- `officeqa-pro133-v9-plumbing/tests/test_officeqa_pack.py`: 6 regression tests.
- `officeqa-pro133-v9-plumbing/tests/test_officeqa_tables.py`: internal-error CLI assertion.
- Requests: `officeqa-v9-design/wave1/S2-pack-review-requests.md` (S4 truncation path, raw command text, lock; slow marker).
