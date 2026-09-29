# S2 — OfficeQA pack: L1 table tools, observation re-derivation, question spec, pack rules

Status: done. All S2 tests pass: 63 fast and 22 slow. The whole `tests/` suite (all streams, without the live-Codex test) also passes: 209 passed.

This report contains no answer values: only counts, timings, file paths and table ids.

Terms used below:
- **L1 / L4**: design layers. L1 is the pack tools the actor can run. L4 is the commit gate that checks a submitted answer.
- **Ledger**: the broker's event log of what the model saw.
- **Observe**: one ledger event for one value the model saw.
- **Compute**: one `calc` run recorded in the ledger.
- **Printing / vintage**: the same table cell as printed in a later bulletin issue, where it may have been revised.
- **FY / CY**: fiscal year / calendar year. The federal fiscal year ran July–June through FY1976 and October–September from FY1977.
- **Offer**: a concrete alternative answer that the gate computes itself, so the actor only has to accept or keep.

## 1. What was built

Everything is under `officeqa-pro133-v9-plumbing/packs/officeqa/`.

| file | lines | contents |
|---|---|---|
| `otables.py` | 2,255 | The hardened prototype (stdlib only). Details below. |
| `cli.py` | 140 | Entry points `find_tables(argv)`, `show_table(argv)`, `printings(argv)` and `build`. Details below. |
| `observe.py` | 449 | `observations(command, output, *, source, call, t, next_ref, tables)`. Details below. |
| `spec.py` | 447 | `question_spec(question) -> QuestionSpec` and `canonical(answer)`. Details below. |
| `rules.py` | 580 | The predicates and offers. See §4. |
| `leak.py` | 85 | `leak_screen(text, released_answers)`, a port of the cards screen. See §4. |
| `seed_rules.json` | 5 rules | `period_match` (priority 25), `later_printing` (30), `flagged_cells` (45), `table_alternatives` (55), `row_coverage` (58). All have scope `pack`, origin `seed`, templates of 105–192 bytes, and pass `harness_core.rules.validate_rule`. |
| `seed_notes.json` | 5 NoteSpecs | The same knowledge as text for the text arms: 234–330 bytes each. `seed_text()` is 1.4 KB. |
| `pack.py` | 108 | `OfficeQAPack`: `name='officeqa'`, `answer_kind='text'`, `tools={find_tables, show_table, printings: 'cli:<fn>'}`. Plus `question_spec`, `canonical`, `numbers`, `observations`, `predicates`, `offers`, `seed_rules`, `seed_text`, `leak_screen`, `task_conventions` (the two published paper rules, close to verbatim), and the extras `seed_notes()`, `usage()`, `tables` and `close()`. |

### `otables.py`: the table analyzer, index and pages

- **Analysis.** The prototype's `analyze` is kept unchanged: realignment VERIFY notes, SHIFT+1 / SPLIT / ROWSPLIT repairs, `Y?` year fill-down, and identity notes. It gains two structured outputs:
  - `identities`: the total column, the columns it sums, the support count and the failing lines;
  - `colflags`: per column, `label_realigned`, `label_unreliable` or `header_shift`.
- **Index.** `build_index(corpus_dir, out_path, files=None)` writes a temporary file, renames it, and sets mode 0444. It adds a `meta` table (version and corpus manifest sha256) and two lookup indexes. Content is deterministic.
- **Read-only access.** `connect_ro` opens the index as `file:…?mode=ro&immutable=1`.
- **Tables class.** `Tables(corpus, index)` offers `find_page`, `show_page`, `printings_page`, `printings_data`, `latest_printing`, `row_cells` and `column_cells`.
  - Each page method returns a `Page`: the exact printed lines plus one `Cell` per displayed data cell. A `Cell` records the index of the line it sits on, its locator, its raw text, its value and its `oq.*` attributes.
  - Because the pages and the observations come from the same code, they cannot drift apart.
- **Page format.** Every `show_table` page:
  - starts with `TABLE <id-or-title> | <file>:<line> | part k | page rows a-b of N`;
  - is at most 8,000 bytes (UTF-8);
  - ends with `continue with --from-row N` when it is cut.
  - On continuation pages the NOTES block is replaced by a one-line pointer to page 1.
  - When the column legend and NOTES would not fit, they are trimmed with a marker.
  - Numeric cells are never cut in the grid. Only text cells longer than 14 characters are shortened, with `…`.
  - `find_tables` and `printings` are capped at the same 8,000 bytes.
- **Error handling.** Errors raise `ToolError` (the CLI exits with code 2). File names are reduced to their basename, so a path cannot leave the corpus.

### `cli.py`: the command-line tools

- Arguments are parsed as in the prototype.
- Paths come from `OFFICEQA_CORPUS` and `TABLES_INDEX`.
- The same argument parsers are reused by `observe.py`.
- Startup is about 70 ms with the venv python and 0.4 s with the frozen `frameworks-v9/actor-python` in a clean environment with `-I`.

### `observe.py`: re-deriving what the model saw

- **Pack tools.**
  - The command is split into simple commands, keeping quotes and removing redirections and prefixes such as `env`, `timeout`, `X=Y` and `cd … &&`.
  - Each tool call is recognised: `show_table …`, `python …/cli.py show_table …`, `python -m packs.officeqa.cli …`, or `…/.harness/bin/show_table`.
  - The call is parsed with the CLI's own parser and re-run.
  - One Observe is emitted per displayed cell whose output line appears complete in what the model saw. This handles `--from-row`, `--cols`, `--match`, `--grep`, `| head` and Codex's head+tail truncation.
  - Loops and invocations the parser cannot read are recovered from the page's first line: `TABLE … | file:line | …` or `printings(row=…, table=…, …)`.
- **Observe attributes.**
  - Always: `oq.table`, `oq.table_ref`, `oq.file`, `oq.issue`, `oq.part`, `oq.col`, `oq.column`, `oq.row`, `oq.section`, `oq.period`, `oq.row_period`, `oq.col_period`, `oq.flags` (a list), `oq.repair`, `oq.identity_fail`, `oq.units`, `oq.page`.
  - For `printings`: also `oq.printing_of` and `oq.issue`.
  - `find_tables` hits carry `oq.kind='table_hit'` and no value.
- **Raw corpus lines.** Each emitted line is verified against the corpus text at that line number. Recognised formats:

  | command | output format |
  |---|---|
  | `rg` / `grep` | `file:line:text` |
  | `grep -n` with a file named in the command | `line:text` |
  | `cat -n` | numbered lines |
  | `officeqa.py read` | printed dict or JSON, lines `N: text` |
  | `officeqa.py search` | excerpt lines, located inside the result's `start_line..end_line` |
  | unnumbered `sed -n 'a,bp'`, `head`, `tail`, `cat`, `awk NR` over corpus files | aligned against the corpus |

  - Each line becomes one Observe with locator `file:line`, text of at most 200 characters, `attrs['values']`, `oq.via`, and `oq.table_ref` when the line lies inside a pipe table.
- **Raw fallback.** Any other complete output line that contains a number becomes an Observe with locator `<source>:L<n>`, so the core grounding checks still see the value.
- **Limits.** At most 400 lines per command. Fragments next to a truncation marker are dropped, including inside one-line dict or JSON outputs. The function never raises.

### `spec.py`: the question spec

It ports and generalises `replay/checkers.py:174`.
- **Answer shape.**
  - `n_values`: stated counts, "both", "in the order of …", `format [..]`, ordinal slots, "slope and intercept", "starting … ending" lists, "a single value".
  - `decimals` per value: ordinal slots or position; significant digits give `None`; "how many" gives `(0,)`.
  - `percent_form`: read from the answer-form phrases outside parentheses.
  - `value_type`: year, integer or number. "how many standard deviations" or "how many percentage points" are not integers.
- **`periods`.** Normalised to `FYyyyy`, `CYyyyy`, `yyyy-mm` or `yyyy`. Year lists and ranges, month ranges, months across year ranges, month lists and quarters are all recognised. Ranges are expanded up to 60 years or 120 months. CPI bases such as "1982-84=100" and the dates of a pinned publication are excluded.
- **`pins_date`.** Set by phrases such as "June 1970 bulletin", "10/1960 Bulletin", "bulletin(s) published in/from …", "published on the last day…", "reported by the U.S. Treasury in <month year>", "as of reported values on the end of the 1982 FY", "reported at the end of FY …" and "exclusively from … bulletins". "Most recently published" does not set it.
- **`keywords`.** Content words, lower case, with a stoplist.

## 2. Verification (exact commands, results)

All commands run from `officeqa-pro133-v9-plumbing/` with `PY=../adapter-comparison-work/.venv/bin/python` (Python 3.12.14, pytest 9.1.1).

1. **Tests.**
   - `$PY -m pytest tests/test_officeqa_pack.py tests/test_officeqa_tables.py -m "not slow"` gives **63 passed in 0.98 s**. The only warning is the unregistered `slow` marker (request 2).
   - `$PY -m pytest tests/test_officeqa_tables.py -m slow` gives **22 passed in 29.2 s**. This includes building the full index into a pytest tmp dir, which takes 21.2 s.
   - `$PY -m pytest tests/ -m "not slow" --ignore=tests/test_v9_codex_live.py` (all streams) gives **209 passed in 54.9 s**.
2. **The 21 structural cases from `run_cases.py`**, ported one test each to `tests/test_officeqa_tables.py::test_case_*`, using the full index.
   - All 21 pass with the same verdicts as the prototype: 17 PASS, and 4 PARTIAL cases (PDO-1, MQ-1, TSO-3, FFO-5) where the test asserts the partial behaviour, meaning the warning is emitted and nothing is silently fixed.
   - New assertions check the structured facts. For example, PDO-1 identity failures appear in `identities.bad`, and 1944_03 and TSO-3 set `label_unreliable`, 1956_07 sets `label_realigned`, and MQ-1 sets `header_shift`.
   - A sweep renders every table of 10 issues, one per decade, through `show_page`. It has no crash, and every page is within 8,000 bytes and has a header line (6.6 s).
3. **Page format tests (fast).** They check:
   - the header line fields;
   - continuation pages (`--from-row`) partition the rows;
   - the byte budget in every mode (default, `--stitch`, `--long`, `--raw`, `--budget 2000/99999`, `printings`, `find_tables`);
   - determinism after clearing all caches;
   - numeric cells are never cut;
   - the index is read-only (an INSERT fails, and the file is not writable);
   - CLI exit codes and messages, including `../../etc/passwd` rejected, a missing index, and bad arguments;
   - CLI startup under 1.5 s in a subprocess.
4. **Observation re-derivation tests.** They check:
   - page cells equal observations, attribute by attribute;
   - `| head -n K` yields exactly the cells on the first K lines;
   - with Codex truncation, only the cells on complete lines are observed;
   - `--from-row` pages yield disjoint cells;
   - 4 invocation forms plus a `for` loop, which is recovered through the page's first line;
   - `printings` and `find_tables` observations;
   - `rg`, `grep -n`, `sed -n`, `cat -n`, `officeqa read` (repr and JSON, with and without truncation) and `officeqa search` excerpts;
   - a tampered line becomes a raw line, never a corpus locator;
   - the 400-line cap;
   - garbage input never raises.
5. **Replay over real v8 outputs.** Every `function_call_output` in the final requests of all 133 none-arm and 133 cards-arm runs went through `harness_core.ledger.normalize_output` and then `pack.observations`. Script: scratchpad `v8cov.py`.

   | arm | outputs | observes | avg time | max time | outputs > 0.5 s |
   |---|---|---|---|---|---|
   | none | 2,779 | 69,698 | 10 ms | 0.47 s | 0 |
   | cards | 2,832 | 69,867 | 8 ms | 0.46 s | 0 |

   Commands with at least one corpus-located observe (none arm):

   | command | with a located observe / total |
   |---|---|
   | `sed` | 137/159 |
   | `grep` | 124/175 |
   | `read` | 295/505 |
   | `search` | 202/636 |

   The rest, such as `cat README`, `find` and `ls`, have no corpus lines. Their numbers reach the ledger as raw lines.
6. **Question spec.**
   - Hand-checked against the question text for 29 questions: Q1, 7, 9, 11, 12, 13, 14, 16, 18, 22, 25, 26, 37, 41, 49, 53, 59, 63, 67, 78, 86, 88, 91, 95, 116, 122, 124, 125, 130. These are in `test_question_spec_hand_checked` and all pass.
   - Distribution over all 133 questions:

     | field | distribution |
     |---|---|
     | `n_values` | 1 → 115, 2 → 13, 3 → 4, 12 → 1; list format 18 |
     | `decimals` | stated for 114; not stated for 17; `None` (significant digits or ambiguous) for 2 |
     | decimals values | 2 dp ×46, 3 ×29, 0 ×22, 1 ×12, 4 ×9, 6 ×2, 5 ×1 |
     | `percent_form` | percent 14, decimal 7, unspecified 112 |
     | `value_type` | number 125, integer 6, year 2 |
     | `pins_date` | 26: Q4, 11, 12, 18, 24, 25, 27, 33, 35, 40, 41, 47, 49, 52, 63, 73, 79, 87, 97, 101, 105, 108, 125, 126, 129, 132 |
     | periods | none for 6; FY for 25; CY for 30; months for 69; bare years only for 13; median 3 per question (max 90, Q17) |
     | keywords | mean 14.8 |

   - Q95 is not pinned, although it contains "reported in December 2000". This follows effect.md: its later printing was the right one.
7. **Predicates and offers.** Unit tests use synthetic events, a stub tables object and an in-process fake `ComputeRunner`. They cover:
   - links through ledger links, the fallback link, typed answers, and calc-of-calc chains;
   - `r` and `p` never trigger `flagged_cells`, and a `Y?` column resolved by the row year is not flagged;
   - later printing: fires when the value differs; silent when the value is the same, when a date is pinned, for estimates, and when the printing is ambiguous;
   - the offer re-runs the calc (`118.5` from a substituted input) and keeps `$1,079 million` and `1.079` formats;
   - FY/CY consistency rules, including the FY1976 → FY1977 boundary;
   - `period_mismatch` with the alternative cell and its offer;
   - silence for indirect periods ("the calendar year that …") and when no alternative exists;
   - `better_table` on an in-memory index;
   - `row_coverage` with except-items and total columns;
   - the seed rules through `harness_core.engine.RuleEngine`: the text renders in at most 200 bytes, and the offer is attached;
   - speed: all 5 predicates take under 2 s on a 3,000-observation ledger.
8. **End to end.** Three scenarios run through `harness_core.ledger.Ledger`, `harness_core.gate.Gate`, this pack, the real corpus and a small index:

   | scenario | result |
   |---|---|
   | Judiciary January 1987 read from the 1987_06 FFO-3 table, then `calc`, then `submit` | `later_printing` fires with the 1988_03 printing and an Accept offer (< 2 s) |
   | SBN-3 FY1999 cell used for "calendar year 1999" | `period_match` offers the CY1999 cell of the same column. Resubmitting the offer clears the finding. |
   | AY-1 1970_06 `Y?` cell | `flagged_cells` fires, with "Anchor the year with a SIBLINGS printing …" |
9. **Offline fire rates on v8.** The pack seed rules were run on each submitted final answer in the 3 arms' final requests (386 episodes). Right/wrong labels come only from `officeqa-divergence-analysis/index.json`. Script: scratchpad `v8rules2.py`.
   - v8 had no `calc` frames, so only answers typed straight from a displayed cell can be linked: 26 of 386.
   - The union of the pack rules fired on **5 wrong and 0 right** episodes: `table_alternatives` 5, `period_match` 2.
   - Maximum rule evaluation time was 0.82 s.
   - This checks precision only. How often the rules fire in v9 depends on calc links.

The full tables index was built once at `frameworks-v9/officeqa-tables-index/tables.sqlite`:
- command: `$PY -m packs.officeqa.cli build --corpus ../opus-officeqa-133/corpus --out ../frameworks-v9/officeqa-tables-index/tables.sqlite`;
- 697 files, 95,053 tables;
- 20.7 s wall time, 526 MB peak memory;
- 146,792,448 bytes, mode 0444;
- `meta.corpus_manifest_sha256 = 8b4d7329…c150b0`.

It is not in the source tree.

## 3. Interface deviations from contracts.py and the DESIGN

- **No printed cell tags.** The DESIGN (L1) describes `⟦c<id> …⟧` cell tags in tool output. As the task asked, the tools print none. Pages describe themselves through the header line, and the ledger re-derives cells by re-running the tool. This keeps the model-visible text compact, and a tag can never disagree with a cell.
- **Pack extras.** These are not in `contracts.Pack`:
  - `OfficeQAPack(corpus=None, index=None)` constructor;
  - `.tables` (the ledger and rules use it);
  - `.close()`;
  - `.seed_notes() -> list[NoteSpec]`;
  - `.usage()` (README block).

  `tools` values are `'cli:<function>'`, relative to `packs.officeqa`.
- **`numbers()`** is `harness_core.numeric.numbers_in`, oriented to answers and consistent with the core's tokens. Observation `values` use the table-aware `otables.numbers`, so `2,659r` and `r 16,311` parse correctly.
- **Extra Observe attributes.** Beyond the documented `oq.*` keys: `oq.table_ref`, `oq.col`, `oq.row`, `oq.section`, `oq.row_period`, `oq.col_period`, `oq.units`, `oq.part`, `oq.kind`, `oq.via`, `oq.summary`, `oq.raw`.
- **Non-corpus locators.** Raw fallback lines use the locator `<source>:L<n>`. This mirrors `harness_core.observe.raw_line_observations`.
- **`QuestionSpec` conventions:**
  - `decimals`: its length equals `n_values` when that is known; a 1-tuple means "every value" for a list of unknown length; `()` means not stated.
  - `periods`: a bare `yyyy` means the basis is not stated.
- **Predicate names and binding keys** are documented in the `rules.py` docstring. Every binding also carries the core-style keys:
  - `value`: the candidate token with its index;
  - `compute` `{ref, depth}`;
  - `substitutions` `[{from: observe ref, value, label}]`, which `harness_core.offers.recompute` consumes.
- **Offers.** `substitute_printing` and `substitute_period` share one implementation.
  - They re-run through the core `recompute` only when the calc that produced the answer itself read the cell (depth 1).
  - An answer typed straight from a cell is replaced in place.
- **Leak screen.** It drops the cards 8-gram question-copy check, because the signature receives answers, not questions. Findings name the answer's index, never its value.

## 4. Known gaps and risks

1. **Header realignment.** The prototype's partial cases stay partial: MQ-1 shift, TSO spurious label, garbled PDO-1 year labels, the fused FFO-5 cell. They are warned about and flagged (`header_shift`, `label_unreliable`, `identity_fail`), but not fixed.
2. **`later_printing` covers only rows with a period.**
   - The row must have an FY/CY year or `yyyy-mm` period. The same row is found by exact period, and the column by its label without footnote marks plus the printed `(n)` number.
   - Transposed tables (country rows × month columns, the CM tables) are not checked.
   - The scan is bounded (at most 48 table parts, newest first). A very long window in a monthly-issue era may therefore report an earlier printing as the latest. Measured cold worst case: 0.74 s per cell. With the default of 3 cells, one rule stays under 2 s.
3. **Links in v9 depend on `calc`.** A number the model computes in its head links to nothing. The core `recompute_final` rule is meant to push those computations into `calc`. Typed links need an exact value with at least 3 significant digits, which avoids coincidental matches.
4. **Parser coverage.**
   - `grep -n` loops over several files, where the file names are not printed, give raw lines only.
   - Search excerpts with duplicate lines inside one chunk map to the first occurrence.
   - Custom python prints give raw lines.
5. **The spec parser was tuned on the 133 development questions** (generality review P7). Freeze it before any confirmation benchmark.
   - Known weak spots:
     - Mixed number and word answers (Q103; request 3).
     - Publication pins that are only implied, such as Q45's "February month-end" reports. These are not pins.
     - Period expansion is generous: "between 1934 and 1946" expands to every year in between. This lowers `period_match` recall and keeps its precision.
   - `period_match` is silent on indirect periods ("the year that …", "during which …").
6. **Thresholds are set by hand and are candidates for the learner.**
   - `better_table` requires at least 3 matched words, 2 more than the table used, and 1.5 times as many.
   - `row_coverage` requires at least 2 used cells from one row, in a sum question.
   - `flagged_cells` defaults to repair, `Y?` and `identity_fail`. It never fires on plain `r`/`p` marks (effect review P6).
7. **The ledger's 400-observation cap per output** can drop cells of wide pages (request 1).
