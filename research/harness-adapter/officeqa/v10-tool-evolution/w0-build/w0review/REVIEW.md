# W0 code review (task V) — tasks T (tools) and X (texts) against W0-PROPOSAL.md

Scope: scratch copy `officeqa-pro133-v10-w0work` only; read-only review. Baselines: `officeqa-pro133-v9-plumbing`
(tools/tests unchanged between v9 and v10 plumbing) and `officeqa-pro133-v10-plumbing` (pack.py/observe.py).
Corpus `v9-p1-a/prepared/agent/corpus`, indexes `frameworks-v9` (v9.1) and `frameworks-v10` (built by T). No answer
values appear in this file; every example is a table id, issue, line number or byte count.

Glossary (for a beginner): *FTS* = full-text search (SQLite FTS5 index over titles/headers/row labels);
*OR fallback* = when no table has every query word, list tables that have some of them, best first;
*SEED.md* = the advice text the model reads first; *USAGE.md* = the tool cheat-sheet (harness arms only);
*parity* = both SEEDs carry the same knowledge in different words.

## Test results (run by the reviewer)

| suite | result |
|---|---|
| `tests/test_officeqa_tables.py` + `tests/test_officeqa_pack.py`, fast | 88 passed, 29 skipped (slow), 1.6 s |
| `tests/test_officeqa_tables.py -m slow` with `V9_SLOW=1`, corpus + v10 index | 29 passed, 47 s |
| `tests/test_v10_arms.py` | 4 failed, 53 passed (committed adapters not regenerated; see F3) |
| `tests/test_v10_kit.py` | passes |

## Checks the brief asked for

1. **Additive formats / observe.py**: `find_tables(...)` first line, `TABLE ... | page rows a-b of N` and
   `printings(row=..., table=..., period=..., col=...): N table parts searched in issues A..B` are unchanged; the
   new `| section='...' | all-periods` suffix is appended after the frozen part and `observe.PRINTINGS_RX` accepts
   both forms (test `test_w0_observe_recovers_printings_pages_carrying_the_new_options`). Every new option
   (`--title-only`, repeated `--grep`, `--period`, `--section`, `--all-periods`) parses through `cli.parser`, which
   observe.py reuses. OK.
2. **Development-set leakage in code**: added lines of otables.py/cli.py/observe.py name no table id beyond the
   pre-existing `FFO-3` help example, no UID, no issue file. One nit: `GREP_HINT` carries the example period
   `(1942-12)` (the proposal's own example, F7). Named tables/years occur only in tests, as allowed.
3. **Page budgets**: every probed page ≤ 8,000 B, including the group-lines-only cut (`total --limit 200`:
   7,933 B, 52 of 6,071 groups), `--all` on 1,167 groups, and the SUMMARY-first printings cut. OK.
4. **W0-8 strict trigger**: `_label_shift` requires k+1 labels for k data-bearing columns, ordinals (1)..(k),
   exactly one unnumbered parent-only label, last column empty in every data row (`empty_cols`), and the label-claimed
   identity failing (< 80 % on ≥ 3 rows) while the shifted one holds; both mappings are printed (`LABELSHIFT-1 ->`),
   the analysis keeps the printed labels; harm cases (FFO-2 2011_09, MQ-1, TSO-1, OFS-2 1977) do not fire. Matches
   §1.5. **W0-1 fallback**: ranked by distinct tokens, title/header before row-label-only, old where/kind score
   inside ties, capped at 3,000 parts, at most `--limit` groups — but the cap is applied by oldest rowid (F2).
5. **Texts**: both SEEDs equal the proposal's ```markdown blocks byte for byte (test re-reads the proposal);
   harness 4,066 B / prompt 4,092 B with the final newline (proposal: 4,065 / 4,091 without it), ≤ SEED_MAX 4,096.
   K1..K13 sentences are raw substrings of their SEED; prompt SEED names none of the four tools; harness SEED names
   all four. USAGE 2,273 B + calc lines = 2,513 B ≤ 3,072; `arms.usage_violations` = [] against the current
   sources. Deviation none; two proposal-inherited issues noted (F8).
6. **Performance** (best of 3, subprocess incl. interpreter start; v9 tools/v9 index -> W0 tools/v10 index):
   `find_tables "judicial branch"` 56 -> 87 ms; `"federal securities"` 586 -> 879; `"total"` 1,737 -> 2,296;
   `show_table 2004_03:728` 44 -> 62 (`--long` 44 -> 62); `printings FD-1 FY2000 1969-2025` (317 issues) 1,569 ->
   1,588; `printings FFO-3 2003-01 --col (2)` 460 -> 478; `printings AY-1 1964-02 1964-1975` 1,810 -> 2,051.
   Two calls exceed 2x: `show_table 2007_03:2301` (header-shift table) 57 -> 146 ms (2.6x, F5) and the Y? table
   `show_table 1970_06:4946` 47 -> 90 ms (1.9x). The OR fallback (new path) costs ~215 ms. W0 tools open the
   v9.1 index unchanged (same timings; `[section]` block absent).

## Findings (most severe first)

**F1 (major) `packs/officeqa/tools/otables.py` `_sections` (~line 2455): the W0-12 `[section]` block misses its
target without `--years`.** The FTS OR query carries `LIMIT 4000` with no ORDER BY, so SQLite returns the first
4,000 matches by rowid (= oldest issues) and only those are scored. For the proposal's own phrase
`find_tables "treasury financing operations auction notes"` the five tokens match 10,605 of 27,889 sections; the
page lists 1945-1960 sections and the 1988_03:1688 section (UID0215-type) is absent. The test passes only because it
pins `years="1988"`. Fix: drop the LIMIT (the `s` table is 27,889 rows; scoring them in Python costs a few ms) or
`ORDER BY bm25(fs)`; add the same test without `years`.

**F2 (major) otables.py `_find_partial` (~line 2290): the 3,000-part cap keeps the oldest parts of a tier, so the
group lines of the fallback are computed on truncated groups.** Ties inside `(-ntok, -th, rid)` are broken by
ascending rid = oldest issue first. `find_tables "zzqxv total"` shows as its top group `38 issues 1939_01..1942_03`
while the AND query for the same table shows `429 issues 1939_01..2025_09`; the `latest 3` parts, the issue and
as-of spans (the tags §1.4 says solved UID0154/UID0170), the size tie-break and the `match:k of n` count (a union
over the truncated parts) are all wrong when a tier exceeds the cap, and the first line says `3000 table parts`
(`npartial` is computed and never printed). Fix: cap after grouping (fetch `rid, tid, tid_raw, title, section,
issue` for every matched rid — ≤ 86 k small rows — group, rank, then fetch full rows for the top `--limit`
groups), or at least break ties by newest rid and print `3000 of {npartial} matching parts kept`.

**F3 (major) committed adapters and two arms tests: 4 failures in `tests/test_v10_arms.py`.**
`test_revision_is_sha12...` (revision aea4b14af57f != 720b7ba30245), `test_committed_w0_equals_generated...`
("prompt-w0 differs from the generated W0: ['SEED.md']"), `test_check_adapter_accepts_w0...` ("adapter.json
revision 'w0' on a tree that is not the generated W0"), and `test_seed_parity_checklist...` whose `Broken` helper
appends a line to a prompt SEED that now has 4 B of headroom (fails on size, "4116 B", before the tool-name check).
All four are listed in `W0-CONTENT.md` §7 but not done. Fix before merge: regenerate `adapters/harness-w0` and
`adapters/prompt-w0` (`arms.py build`), update the `Broken`/`Missing` helper cases as §7 suggests, delete the now
unread `packs/officeqa/seed_notes.json`.

**F4 (minor) otables.py `panel_stub` (~line 2050): `_BANNER_NOISE_RX` is applied to the space-squashed string
with no word bounds.** `'Contract authority' -> 'ractauthority'`, `'Marketable' -> 'ketable'`, `'Summary' ->
'sumy'`, `'Separate accounts' -> 'arateaccounts'`. The key is internal (the SUMMARY prints the real section), so
the effect is a collision risk and a banner whose words are all noise-substrings keying as '' (= "no panel").
Fix: strip whole tokens (`\b...\b` on the normalised, still-spaced text) and squash afterwards.

**F5 (minor) otables.py `shift_sibling` / `_anchor` (~lines 2130-2180): sibling searches make two `show_table`
cases 1.9-2.6x slower.** The header-shift page analyses up to `ANCHOR_SIBLINGS = 6` nearby parts (any width,
shifted neighbours skipped) and the `_memo` cache is per `Tables` instance, which the CLI recreates per call.
Absolute cost is small (146 ms) but exceeds the 2x rule. Fix: try same-width siblings first and stop after 3, or
document the exception in PREREG.

**F6 (minor) otables.py `_no_match_lines` (~line 2900): a non-id `--table` hint with zero parts inside the window
prints `no table matches --table '<title words>': ... check the id`, although the table exists in other issues**
(`printings --table "Summary by Months and Calendar Years" --row 1942 --years 2010-2011`). Fix: for non-id hints run
the title FTS over all issues and print the issue span line (`... is printed in issues A..B; no issue in
2010..2011 prints it`) as the id path does.

**F7 (minor) otables.py `GREP_HINT` (~line 296): hard-coded example period `(1942-12)` in model-visible text.**
It is the proposal's wording but a fixed year near a development item; print the table's own first filled period
(`e.g. {a0['rows'][0]['period']}`) instead.

**F8 (minor) proposal-inherited text issues (`pack.py` HARNESS_SEED, byte-pinned): the harness SEED says "retry
with fewer words and `--rows`", but W0-1 made `--rows` a no-op default; and "on 0 groups" is now rarer because
the OR fallback fills that case.** Not fixable in pack.py alone (the byte-for-byte test would fail); amend the
proposal (e.g. "retry with fewer words") and both together.

**F9 (minor) `tests/test_officeqa_pack.py` `PENDING_OPTIONS = {"--title-only", "--section"}` is stale now that
T's options exist in the sources**; set it to `set()` so `set(_OPTION.findall(usage)) - options` is checked strictly.

## Implemented vs deferred (as observed)

W0-1..W0-12 all present in code and tested (12 fast + 7 slow tests plus the two arms-facing pack tests). Deferred
per proposal and confirmed absent: `--rows A..B` range mode, T7 rule 3, T8/T9/T14/T17, T5, T18, T19. Index:
`frameworks-v10/officeqa-tables-index/tables.sqlite` (otables-index-v10, 697 files, 95,053 tables, 27,889
sections, 0444); otables.py is 160,933 B of the 196,608 B `TOOL_FILE_MAX` (35 KB headroom for the learner).
