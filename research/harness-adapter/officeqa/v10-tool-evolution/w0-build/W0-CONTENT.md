# W0-CONTENT: the W0 texts (task X) and the text-only notes for the real v10 tree

Source of every text: `officeqa-v10-analysis/synthesis/W0-PROPOSAL.md` §(a)-(g). This file records what
`packs/officeqa/pack.py` now carries, which USAGE.md lines depend on which tool change of task T, and the
text-only items that belong to files task X does not own (README environment section, controls). No answer
values appear here.

Files changed by task X (scratch copy `officeqa-pro133-v10-w0work`):
- `packs/officeqa/pack.py`: `HARNESS_SEED`, `PROMPT_SEED` (the §(a)/(b) texts), `SEED_KNOWLEDGE` (K1..K13),
  `USAGE` (the §(d) 'USAGE.md' text), `seed_notes()` returns `[]`, `table_notes()` removed.
- `tests/test_officeqa_pack.py`: three new tests (proposal byte-for-byte drift check, parity/byte caps/tool-name
  absence, USAGE line rule and byte cap); the two stale tests that called `seed_text()` / `tool_wrappers()` fixed.
- `packs/officeqa/seed_notes.json`: **no longer read by pack.py**, left untouched (not owned once unread); the
  owner may delete it. Its five v9 sentences are superseded by the SEED bullets below.

Beginner's glossary for this file: *SEED.md* = the advice text shown to the model at the start of every episode;
*USAGE.md* = the tool cheat-sheet appended to the README of the harness arms only; *parity* = the two SEEDs
carry the same knowledge in different words (one has tools, the other only `rg`/`sed`/`python`);
*exposure record* = which development questions each change was designed from (so the report can also score
"all questions except those").

---

## 1. SEED texts (pack.py `HARNESS_SEED`, `PROMPT_SEED`)

| | bytes (with the final newline) | proposal's count (without it) | cap |
|---|---|---|---|
| harness SEED.md | 4,053 (4,066 before amendment F8) | 4,065 | 4,096 |
| prompt SEED.md | 4,092 | 4,091 | 4,096 |

Both texts are the proposal's ```markdown blocks byte for byte, after the one amendment the review (finding F8)
asked for: the harness bullet "retry with fewer words and `--rows`" reads "retry with fewer words" (W0-1 made
`--rows` a no-op default). The proposal file is read-only for this tree, so the drift test
`test_seed_texts_are_the_proposal_texts_byte_for_byte` applies `SEED_AMENDMENTS` (old text -> new text, each
required to occur exactly once) before comparing; the proposal's owner should fold the amendment into §(a). Each
bullet is one line of SEED.md (longest bullet 568 B). The prompt SEED contains none of `find_tables`,
`show_table`, `printings`, `calc` (asserted by `arms.names_tools` and by a plain substring test); the harness SEED
names all four. Headroom is 43 B (harness) and 4 B (prompt): any addition to the prompt SEED must replace words,
not append.

Parity checklist `SEED_KNOWLEDGE` (ids are what `arms.check_w0`/`knowledge_missing` report):

| K | id | harness sentence (prompt sentence when different) |
|---|---|---|
| K1 | `compute_with_code` | "Compute every final number with `calc`, not in your head" (prompt: `python`) |
| K2 | `provenance` | "print the line it comes from, so the number in your calculation is the number in the source" (prompt: "print the line it comes from with `sed -n`, so the number you compute with is the number in the source") |
| K3 | `answer_count` | "The final line is a number or a list, never a sentence" |
| K4 | `answer_form` | "Multiply by 100 only when the answer clause says percent, percentage or %" |
| K5 | `alternatives` | "never average. Candidates include other tables and narrative sections" (the table's two quoted fragments; they are adjacent in both SEEDs, so one sentence covers both) |
| K6 | `latest_printing` | "Unless the question names an issue, publication date, quotation date or "as of" date" |
| K7 | `ocr_variant` | "is an OCR variant, not a revision" |
| K8 | `period_match` | "a fiscal year has exactly 12 month rows" |
| K9 | `flagged_rows` | "map the numbered labels (1)..(n) onto the n numeric columns in order" |
| K10 | `table_choice` | "for "total X" find the summary table with an "X > Total" column" |
| K11 | `entity_column` | "Entity = its own column" |
| K12 | `corpus_boundary` | "Corpus boundary: the corpus is Treasury Bulletins only" |
| K13 | `narrative_sections` | "may mean narrative text" |

Dropped from both SEEDs, per the proposal: the "Loops" sentence (a control, §(e)), the FY-definition clause, the
"For a sum over the columns of one row" item (replaced by K11), the "scope pins" clause ("for the years
1940-1949": UID0007's wording) and the publisher/first-table clause (UID0211's wording).

---

## 2. USAGE.md (pack.py `USAGE`; 2,273 B + `arms.CALC_USAGE` 239 B = 2,513 B of the 3,072 B cap)

The USAGE line rule (`arms.usage_violations`): every non-empty, non-heading line names a manifest tool or an
`--option` that occurs in the tool sources. Every line below names a tool or an option that exists in the tool
sources (task T's `--title-only` (W0-1) and `--section` (W0-3) included), so `PENDING_OPTIONS` in the test is
now the empty set and the rule is checked strictly.

Per-line dependency (drop or replace the line when the change is deferred):

| line | text (abridged) | depends on | if deferred |
|---|---|---|---|
| L01 | Table tools ... 8,000 bytes; one query per call | none (v9 + TOOLS §Usage-text) | keep |
| L02 | `find_tables "<words or table id>" [--years] [--limit 25] [--all] [--title-only]` | `--title-only`: W0-1 | delete `[--title-only]` |
| L03-L05 | matches every word (prefix, 4+ letters) in title, header or row labels; `--rows` default; `--title-only`; partial matches; `match:...` | W0-1 | replace the three lines by the v9 line: `find tables by id (FFO-3) or by words in the title or headers; --rows also searches row labels` |
| L06-L07 | group lines first (`[FD-1] title | issues | rows | as-of`), latest 3 parts, `--all` 12 per group, `--years`/`--limit` narrow | W0-2 | replace by: `find_tables lists the latest 3 parts per group; --all lists every part; --years and --limit narrow a cut page` |
| L08 | `show_table <file> <line> [...] [--period FY2021] [...]` | `--period`: W0-6 | delete `[--period FY2021]` |
| L09-L10 | takes `<file> <line>` (not file:line); NOTES on every page | none (TOOLS §Usage-text; W0-7 only changes help text) | keep |
| L11-L12 | `--grep` substring of label/period/section, `'|' = OR`, not a regex, not a column filter; `--match` | `'|' = OR`: W0-6; the rest v9 behaviour | drop `('|' = OR)` |
| L13 | `--period FY2021` keeps the rows of that year (12 rows for a fiscal year) | W0-6 | delete the line |
| L14-L15 | page header; `--from-row` is 0-based, pass the printed number; `--stitch` | none (v9 behaviour; W0-7 help text) | keep |
| L16 | `printings --table ... [--section <words>]` | `--section`: W0-3 | delete `[--section <words>]` |
| L17-L18 | SUMMARY first; printings has no `--all` | "SUMMARY first": W0-11 | replace "SUMMARY first: revisions, first" by "with a SUMMARY: revisions, first" |
| L19-L20 | `--row` period or label, `--col` the column; swap for periods-as-columns tables; `--section` keeps one panel | swap: none (T12 README fact); `--section`: W0-3 | drop the `--section` clause after the semicolon |
| L21 | Quote multi-word values | none | keep |
| L22 | calc: compute every candidate in one run | none | keep |

Not in USAGE.md (belongs to the README environment section, §3 below): absolute corpus path, `docs.search`
result shape, `find -printf`/`cat -A`, `statistics.NormalDist`. Task T's draft cli.py help also names
`--all-periods` (W0-11); USAGE.md does not mention it (the proposal's USAGE list does not either), so nothing
depends on it.

---

## 3. Base README 'environment' additions (both architectures; text for the real tree, not applied here)

Target: `kit.readme_l0` (the `# Environment` bullet list; `{corpus}` is the absolute corpus path the function
already interpolates) and `campaign.HELPER_README` (the search example). These are environment facts, not advice,
so they live in the README that every arm sees, and count toward no SEED/USAGE budget.

Append to the `# Environment` list of `kit.readme_l0` (after the corpus/index bullets):

```
- `rg` and `sed` need the absolute path of a corpus file; a bare file name is not found from this workspace:
  rg -n -i "phrase" {corpus}
  sed -n '830,860p' {corpus}/treasury_bulletin_1988_03.txt
- This is macOS: `find -printf` and `cat -A` do not exist (use `find ... -exec stat`, `cat -v`, or python).
- scipy and statsmodels are not installed. For normal-distribution values use the standard library:
  from statistics import NormalDist; NormalDist().cdf(x); NormalDist().inv_cdf(p)
```

Replace the example block of `campaign.HELPER_README` by:

```python
from pathlib import Path
from officeqa import OfficeQACorpus
docs = OfficeQACorpus(Path({corpus!r}), Path({index!r}))
hits = docs.search(query="text", limit=8, filename_glob="*.txt")["results"]   # a dict: {"query", "results", "query_tokens"}
for h in hits:
    print(h["filename"], h["start_line"], h["end_line"], h["excerpt"])          # excerpt = the matched lines
docs.read(filename="document.txt", start_line=1, num_lines=80)
docs.list_files(pattern="*.txt", limit=100, offset=0)
```

and the sentence after it by: "Search returns a dict whose `results` list holds ranked excerpts with filename
and line anchors (`start_line`..`end_line`); read returns numbered document lines. The helper contains no
answers or source hints. You may also read the files directly (rg, sed, Python)." (The result keys are those of
`officeqa_tools.OfficeQACorpus.search`: `filename`, `start_line`, `end_line`, `excerpt`, `rank`; TOOLS.md's
`h["snippet"]` is wrong, the key is `excerpt`.)

Tests to touch when applying: `tests/test_v10_kit.py::test_readme_l0_identical_for_every_arm` (still holds) and
any test pinning `HELPER_README` bytes in `compose_readme`'s byte report.

---

## 4. Controls: implementation note for `controller_runtime.py` (text only; §(e) of the proposal)

`controls.json` values stay the design's: `{"loop_guard": true, "loop_threshold": 3, "loop_notes_max": 3,
"splice": true, "ledger_splice_bytes": 8000, "gate": false, "enforce_requery_max": 0}` plus the new knob below.

1. **Remove the `self.noted` skip** (`MechController._loop_guard`, the `if cmd in self.noted: ... continue`
   branch, controller_runtime.py ~573-577). Today a command that received one loop note is never noted again
   (UID0029 b repeated the noted command ~136 more times). New behaviour: `self.noted[cmd]` holds the count at
   the last note; when `count - self.noted[cmd] >= loop_threshold`, note it again (same `_loop_text`, `loop_notes
   += 1`, decision `loop_note`). So a command is re-noted every `loop_threshold` further repeats.
2. **Hard block after the notes are used up, with a 20-line echo.** Gate off, once `loop_notes >=
   loop_notes_max`, every further issue of a command whose (text, output sha256) pair repeats a previous call
   (`ledger.command_output_count(cmd, sha) > 1`, i.e. `_identical(issued)`) gets the block insert, not only the
   first (drop the once-per-command `self.blocked` guard, or key it by (cmd, sha) and reset it when the output
   changes). The message becomes
   `blocked: identical output already shown for: <cmd> (call N, k repeats)` + the first 20 lines of that output +
   `LOOP_OPTIONS_NOGATE`. The controller does not keep output text today (the ledger stores only
   `output_sha256`/`output_bytes`), so cache the head when the output is ingested: in `on_request`, for each
   `function_call_output` item of `view.codex_input` whose call_id maps to a command, store
   `self.output_heads[(cmd, sha)] = first 20 lines of normalize_output(text).body` (cap ~2,000 B; one entry per
   pair), or add a `head` field to `ledger._output` and read it back through a small ledger method. Update
   `HARD_BLOCK_TEMPLATE`/`fixed_templates` (the template bytes are part of the model-visible budget that
   `arms.harness_fixed_text` and `learner_common.controller_template_bytes` count; a test pins their equality),
   and `HARD_BLOCK_PREFIX` stays the prefix that `summary_extra` counts as `hard_blocks`.
   Why the echo: after a Codex compaction the earlier output is gone from the context; a bare "already shown"
   leaves the model blind (UID0029 b's compaction summary carried a wrong belief). Risk 5 of §(g).
3. **`empty_output_retry` knob** (new `CONTROLS_DEFAULTS` key, default 1, range (0, 1); `checked_controls` must
   accept it and `arms.check_adapter`/PREREG record it). Semantics: a manifest-tool command (name in the served
   manifest) that returns exit 0 and an empty body (`normalize_output(...).body.strip() == ""`, `output_bytes` 0)
   is a capture loss, not evidence. Cheapest form (controller insert, no broker change): on the next request
   insert `[harness] <cmd> printed nothing (exit 0); output lost, run it again`, at most `empty_output_retry`
   times per command, and do not count that re-run toward the loop threshold (the pair (cmd, empty-sha) is
   exempt once). Fuller form, only if the kit wrapper is cheap to change: the tool wrapper script written by
   `kit.build_kit` re-executes the tool once when stdout is empty and the exit code is 0, then prints the line
   above. Evidence: 5 capture losses in the pilot, 0 grade effect, so this is optional.
4. Unchanged: the gate-off loop templates (no `submit` mention), the ledger splice after compaction, the 8,000-byte
   page cap, T = 0, the 3,600 s budget. The "Loops" advice sentence is gone from both SEEDs (0 kept-and-helped).

---

## 5. Exposure record (per W0 change; copied from W0-PROPOSAL §(d); `*` = issue-47 disputed item)

Issue-47 items: UID0007, UID0154, UID0155, UID0213, UID0237. Every contrast in the report is computed on all 50
questions and excluding the items listed for the changes under test.

| change | what | designed from (task ids) |
|---|---|---|
| W0-1 | `find_tables` scope (title+header+row labels), prefix tokens, ranked OR fallback, `match:` field, `--title-only` | UID0182, UID0029, UID0133, UID0180, UID0218, UID0237*, UID0149; zero-hit evidence UID0225, UID0117, UID0211, UID0170, UID0077, UID0243, UID0097, UID0224, UID0245 |
| W0-2 | `find_tables` group-first listing, row/as-of spans, `--all` cap 12 per group, cut trailer, `Table N.` merge, `[snapshot]` | UID0243, UID0180, UID0133, UID0135, UID0094, UID0007*, UID0154*, UID0237*, UID0149, UID0220, UID0130 |
| W0-3 | `printings` keys (part, panel stub, column, period, as-of), `--section`, column-index warning, snapshot line | UID0007*, UID0044, UID0170, UID0211, UID0130, UID0220, UID0188, UID0228, UID0165, UID0214, UID0218 |
| W0-4 | `printings` exact SUMMARY values (Decimal-keyed groups, raw text, no `%g`) | UID0180, UID0135, UID0122 |
| W0-5 | `printings` no-match diagnostics (span/last issue, `--col` miss, row window, periods-as-columns, date-keyed rows, intra-word spaces) | UID0114, UID0218, UID0057, UID0097, UID0122, UID0148, UID0109, UID0180, UID0225, UID0170, UID0101 |
| W0-6 | `show_table --grep` semantics and 0-row message, `|` = OR, month-name forms, `--period FY2021`, `--from-row` past-end message | UID0170, UID0224, UID0237*, UID0100, UID0112, UID0102, UID0057, UID0149, UID0214, UID0220, UID0182, UID0007*, UID0245, UID0109 |
| W0-7 | `show_table` non-table line, part hint, figure caption, `--long` hint, 0-based `--from-row` help | UID0094, UID0037, UID0214, UID0154*, UID0029, UID0133, UID0148, UID0112, UID0077, UID0211, UID0225 |
| W0-8 | `show_table` header-shift repair, strict trigger, both mappings printed (rule 1); date-column label repair (rule 2) | UID0165, UID0148, UID0213*, UID0214, UID0077, UID0228, UID0102, UID0243, UID0101, UID0225; harm-tested (no fire) on UID0112 b, UID0130 a, UID0154 b, UID0225 a/b2/c, UID0148 a/c |
| W0-9 | NOTES printed in `--raw` | UID0028, UID0027, UID0225, UID0148 |
| W0-10 | sibling year anchoring (`YEARS ANCHORED from <file:line>`) | UID0028, UID0029, UID0027, UID0117 |
| W0-11 | `printings` SUMMARY first, month keys exact, `--all-periods` | UID0224, UID0062, UID0077, UID0155* (the one right cell that would flip), UID0135, UID0114, UID0122, UID0243, UID0101 |
| W0-12 (optional) | narrative section index (`[section]` block in `find_tables`) | UID0215, UID0133, UID0183, UID0204, UID0037 |
| SEED texts | K1-K13 (§1 above) | TEXT.md verdicts over all 50; single-question support: K11's qualifier clause (UID0096), K4's metric clause (touches UID0237*), K12's wording (UID0019 exchange-rate counter-example) |
| USAGE.md facts | one query per call, `--grep`/`--match`, 0-based `--from-row`, `--row`/`--col` swap, quoting, no `--all`, `<file> <line>` | TOOLS.md §Usage-text (friction in 17-41 questions each; no single item) |

Tests pinned to development items in `tests/test_officeqa_tables.py` (task T; so a later reviewer knows they are
not held-out): UID0182 CM-I-1 2009-2011 (W0-1), UID0028/UID0029 AY-1 1970_06 vs 1970_05 (W0-10), UID0165 OFS-2
2007_03..2010_03 (W0-8), UID0148 MQ-1 1972_05/1974_05 (W0-8 rule 2), UID0007* the Dec 1949 / Feb 1950 summary
tables (W0-3), UID0114 MY-2's end in 2003_03 (W0-5), UID0243 the 1962 `Table 1.` groups (W0-2). Task X's tests pin
no table, year or line: they pin the proposal's bytes, the checklist and the USAGE line rule.

---

## 6. Deliberately left for the learner (W0-PROPOSAL §(f); room for the evolve arms)

Notes (scoped, one question each in the pilot): parenthetical-definition rule (UID0101); encoded answers with
four-digit years (UID0027); acronym glossary "TABs" and the by-issues sub-block (UID0149); group-header
membership (UID0122); same-title tables: publisher parenthetical / first-table convention (UID0211); "a
column-description phrase is not a date pin" (UID0062); one-table ratios (UID0182); regression index origin
(UID0100); stacked-panel sums (UID0044); paraphrased column headers (UID0218); date-window row counts (UID0148);
rate/annualisation conventions (UID0230, UID0240); percent-difference convention (UID0220).

Tool revisions (code the learner can patch; none touch the index): identity-FAILS repair line with the
cross-printing majority (T8); group-header lines and row footings in `--long`/ROWS (T9); series continuation
across a title change and the revision footnote next to `r` values (T14); component-of cross-table note (T17);
`printings --rows A..B` / repeated `--col` range mode and the compact per-issue listing (T13 remainder);
identity-based Total relabel (T7 rule 3); snapshot-aware `printings` beyond the note; `show_table --compare
<issue>`; `find_tables` ranking weights and `--limit` defaults; the thresholds in W0-1/W0-2/W0-8 (plain
constants).

Not learnable this round and not in W0: T5 index completeness, T18 calc helpers (calc is hash-pinned), T19
reference-series lookup.

---

## 7. For the owner of the real tree (things task X could not do from the scratch copy)

Status after the fix pass (see W0-BUILD-REPORT.md): items 1-3 are done in this scratch copy (adapters
regenerated with `arms.py build`, the two helper cases rewritten, `seed_notes.json` deleted) and item 4 no
longer applies (`show_page` accepts `period=`); the integrator only needs to copy the files listed there and
run `arms.py build` once more in the real tree if the copied tool sources differ from the committed adapters.

1. Regenerate the committed adapters after copying pack.py: `python arms.py build` (or the equivalent) so
   `adapters/harness-w0/SEED.md`, `adapters/prompt-w0/SEED.md` and `adapters/harness-w0/tools/USAGE.md` equal the
   pack's texts; until then `arms.check_w0` raises "differs from the generated W0" and
   `tests/test_v10_arms.py::test_committed_w0_equals_generated_and_passes_every_check` /
   `test_check_adapter_accepts_w0_and_revised_candidates` fail.
2. `tests/test_v10_arms.py::test_seed_parity_checklist_and_prompt_seed_names_no_tool` pins the v9 wording in two
   helper cases: `Broken` appends `'- Check sums with calc.\n'` to the prompt SEED (now 4,092 B, so the size check
   fires first: "prompt SEED is 4116 B, over 4096 B") and `Missing` replaces `'Match the period exactly.'` (the new
   sentence reads `Match the period exactly:`; the id it expects, `period_match`, still exists). Suggested edits:
   `Broken`: `.replace('with `python`', 'with `calc`')`; `Missing`: `.replace('exactly 12 month rows', '12 month
   rows')`.
3. `packs/officeqa/seed_notes.json` is unread; delete it or leave it (the kit probe `read_code_other` in
   `tests/test_v10_kit.py` only checks that the path is denied, which holds for a missing file too).
4. Task T's scratch edits: at the time of writing, `tools/cli.py` passes `period=` to `Tables.show_page`, which
   `tools/otables.py` does not accept yet, so 11 corpus-backed tests in `test_officeqa_tables.py` /
   `test_officeqa_pack.py` fail in the scratch copy for that reason alone (they passed before that edit).
