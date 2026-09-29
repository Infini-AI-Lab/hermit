# W0 build report (fix pass after review V and replay R)

Scratch copy: `officeqa-pro133-v10-w0work`. Inputs: `w0review/REVIEW.md` (F1-F9), the replay findings (replay F1-F4,
the rank discrepancies, the missing `w0replay/REPORT.md`), `W0-CONTENT.md` §7. No answer value appears in this
file; every example is a table id, an issue, a line number, a byte count or a masked argv.

## 中文摘要（写给初学者）

这份报告说明 W0（v10 实验的"起始适配器"，即模型一开始拿到的工具和建议文本）在代码审查和回放测试之后做了哪些修正。

- **背景术语**：*find_tables / show_table / printings* 是三个只读的命令行工具，模型用它们在 697 期《Treasury Bulletin》
  的 OCR 文本里找表、看表、比较同一行数据在不同期刊里的印刷值。*索引*（`tables.sqlite`）是预先建好的 SQLite 数据库，
  记录每张表的标题、表头、行标签和叙述性小节标题；*FTS* = full-text search（全文检索），是 SQLite 的一个模块，能按词快速查
  这些字段。*SEED.md* 是给模型看的建议文本；*adapter*（适配器）是打包好的 SEED + 工具 + 使用说明的目录。*replay*（回放）
  指把 v9 试点里模型实际敲过的 2,290 条工具命令，分别用旧工具（v9）和新工具（v10）重跑一遍，逐条比较退出码和页面内容。
- **修了什么**（全部在 `packs/officeqa/tools/otables.py`，除非另注）：
  1. F1：叙述性小节的 `[section]` 检索原来只打分前 4,000 条命中（SQLite 默认按行号取，全是最早年份），目标小节找不到；
     现在全部打分，并把同一标题在多期重复出现的行合并成一行（写出期数范围和最新位置），不再被样板标题吃掉版面。
  2. F2：OR 回退（没有表包含全部查询词时列出"部分匹配"）原来只取最早的 3,000 个表部分再分组，导致期数范围、
     "最新 3 个部分"、匹配计数都错；现在先按轻量列取全部命中分组排序，只为进入页面的组取完整行。
  3. 回放 F1：分组行先打印导致版面不够放"部分块"（file:line）。现在预留前 3 个组的部分块（最多半页），
     版面不够时至少打印顶组的前几个部分并注明还有多少。
  4. 回放 F2：月份键隐藏了按日/按周的行，而且按日查询（如 `--row 1939-Jan. 19`）解析成月后匹配不到。现在按日查询只匹配
     那一天；月份查询在页面上写明隐藏了多少日/周行；查不到时列出该月有哪些日期；诊断里的期间范围过滤掉 OCR 损坏的年份。
  5. 回放 F3：同一序列因横幅 OCR 差异（`series`/`aeries`）被拆成两个"面板"，导致误报"所有印刷相同"。现在若每期只有
     一个横幅，就合并成一个面板并注明横幅有变体；真正的多面板表（同一期有两个横幅）仍然分开。
  6. 回放 F4：`printings --all` 曾被 argparse 当作 `--all-periods` 的缩写接受；现在 `allow_abbrev=False`（`cli.py`）。
  7. F4：面板键去噪按整词进行（`Contract authority` 不再变成 `ractauthority`）。
  8. F5：表头错位的"邻近期刊确认"改为读索引里存的表头，不再解析 5 个邻表；该页从 169 ms 降到 72 ms（v9 52 ms）。
  9. F6：`--table` 给的是标题词而不是表 id 时，超出年份窗口也能给出"该表印于 A..B 期"的诊断。
  10. F7：`--grep` 0 行提示里的示例期间改为该表自己的第一个期间（不再写死 `1942-12`）。
  11. F8：SEED 里的"retry with fewer words and `--rows`"改为"retry with fewer words"（`--rows` 已是默认，属无效建议）。
  12. F3/F9：重新生成了 `adapters/harness-w0`、`adapters/prompt-w0`；改了 `tests/test_v10_arms.py` 的三个辅助用例；
      删除了无人读取的 `packs/officeqa/seed_notes.json`；`PENDING_OPTIONS` 置空。
- **结果**：快测 168 个通过（表工具 31 + pack 68 + arms 21 + kit 36 + gate 12），慢测 29 个通过；回放 2,290 条命令
  0 次退出码回归、0 崩溃、0 超时；提案的伤害测试（表头修复不误触发、右答单元的检索排名不变差）全部通过。

## 1. What is implemented (W0-1..W0-12) and what this pass changed

| item | status | fix-pass change |
|---|---|---|
| W0-1 find_tables scope / prefix / ranked OR fallback / `match:` / `--title-only` | done (task T) | F2: fallback groups every matching part (`_find_partial` light rows + `_hydrate`); `PARTIAL_RIDS` removed; `_group_hits` memoises the group key per distinct title tuple (OR path on a 66,590-part flood: 1,465 -> 846 ms) |
| W0-2 group-first listing, spans, `--all` cap 12, `Table N.` merge, `[snapshot]` | done | replay F1: `_assemble_find` reserves the part blocks of the first `RESERVED_BLOCKS = 3` groups (<= half the page) and prints a truncated top block (`_cut_block`) when even that does not fit; one trailer format `k of N groups listed, parts shown for the first j` |
| W0-3 printings keys (part, panel stub, column, period, as-of), `--section`, column-index warning | done | F4 whole-word stub; replay F3: `_panel_keys` merges banner variants when no issue prints two stubs (`same_panel` for real multi-panel tables); `latest_printing` accepts an OCR-variant stub when the exact one is absent; single-printing keys print their locator on the value line and no first/latest line (snapshot pages 1/3 shorter) |
| W0-4 exact SUMMARY values | done | unchanged |
| W0-5 no-match diagnostics | done | F6: `_hint_where` gives title-word hints the issue-span lines; replay F2: `_period_range` (issue-relative plausibility, `PERIOD_LOOKBACK = 60`), day-level and hidden-daily-rows lines |
| W0-6 `--grep` semantics, `|` OR, `--period FY2021` | done | F7: `grep_hint(a0)` prints the table's own first period |
| W0-7 non-table line, part hint, figure caption, `--long` hint | done | unchanged |
| W0-8 header-shift repair (strict trigger, both mappings) | done | F5: `shift_sibling` reads `_nearby_headers` (index rows) instead of analysing up to 6 siblings; the repair itself is unchanged (harm A: no fire on the 8 right cells, positive control fires) |
| W0-9 NOTES in `--raw` | done | unchanged |
| W0-10 sibling year anchoring | done | unchanged (1970_06:4946 page 85 ms vs v9 45 ms: the anchoring itself; below 2x) |
| W0-11 SUMMARY first, month keys exact, `--all-periods` | done | replay F2: `query_day` (a day-level `--row` matches that day only); a month `--row` prints `N daily/weekly rows of YYYY-MM not listed (...)`; replay F4: `allow_abbrev=False` in `cli._Parser` |
| W0-12 narrative section index | done | F1: `_sections` scores every FTS match (no LIMIT), ranks (tokens, heading tokens, newest issue) and collapses issues repeating one heading (`panel_stub` key) into `[section] H | N issues A..B, latest file:line-end | sub-headings` |
| SEED texts (K1-K13), USAGE.md | done (task X) | F8 amendment in `HARNESS_SEED` (4,053 B; prompt 4,092 B); adapters regenerated; test helper cases rewritten |

Deferred, unchanged from the proposal: `--rows A..B` range mode, T7 rule 3, T8/T9/T14/T17, T5, T18, T19, and the
learner items of W0-CONTENT §6.

## 2. Files changed in this scratch copy

- `packs/officeqa/tools/otables.py` (171,852 B of the 196,608 B `TOOL_FILE_MAX`; index schema and `INDEX_VERSION`
  unchanged, so `frameworks-v10/officeqa-tables-index/tables.sqlite` built by task T needs no rebuild)
- `packs/officeqa/tools/cli.py` (`_Parser.__init__` with `allow_abbrev=False`)
- `packs/officeqa/pack.py` (F8 phrase; comment byte count)
- `packs/officeqa/seed_notes.json` deleted
- `tests/test_officeqa_tables.py` (+11 `test_fix_*` tests, fast, small index; day-level test on PDO-2 1987_09:1674)
- `tests/test_officeqa_pack.py` (`PENDING_OPTIONS = set()`, `SEED_AMENDMENTS`, harness size 4,053)
- `tests/test_v10_arms.py` (`Broken`, `Missing`, `Short` helper cases)
- `adapters/harness-w0/{SEED.md,tools/otables.py,tools/cli.py}`, `adapters/prompt-w0/SEED.md` (regenerated by `arms.py build`)
- `W0-CONTENT.md` (§1 sizes and amendment, §2 PENDING_OPTIONS, §7 status)
- `W0-BUILD-REPORT.md` (this file). `w0replay/REPORT.md` is NOT on disk: the harness refuses report-file writes
  from the subagent (both times); its full text is the replay section of the fix-pass StructuredOutput report and
  should be saved there verbatim by the parent.

Not changed: `packs/officeqa/observe.py` (the review's check 1 holds: every new option parses through `cli.parser`,
the frozen first lines are unchanged), `w0replay/*.py`, `w0review/REVIEW.md`, PREREG.md, PROTOCOL.md.

## 3. Files the integrator must copy into the real tree

The real tree `officeqa-pro133-v10-plumbing` already carried a 04:58 snapshot of this tree's tools (its `otables.py`
has `_panel_keys`, `_hydrate`, `_nearby_headers`, `query_day`, `grep_hint`; its `cli.py` is byte-identical). What it
lacks from this tree: `_cut_block`, the `_assemble_find` loop fix and the three-block reserve (`RESERVED_BLOCKS`),
the 11 new tests, the pack/arms test edits. Copy:

1. `packs/officeqa/tools/otables.py` -> also `adapters/harness-w0/tools/otables.py` (or run `arms.py build`)
2. `packs/officeqa/tools/cli.py` (identical today; copy to be safe)
3. `packs/officeqa/observe.py`: nothing of this pass; keep the real tree's copy (it differs only in the `.kit`/`.harness`
   path comment, which is the real tree's own change)
4. `packs/officeqa/pack.py`: apply the two hunks (the F8 phrase `retry with fewer words.` and the comment) to the real
   tree's file, which has an exposure-record block this scratch copy lacks; do not copy the whole file
5. `tests/test_officeqa_tables.py` (whole file: the real tree's is the 699-line task-T version)
6. `tests/test_officeqa_pack.py`: the `PENDING_OPTIONS = set()` + `SEED_AMENDMENTS` hunk, the `_proposal_blocks`
   amendment loop, and `"harness": 4053`; the `.kit`/`.harness` line is the real tree's own
7. `tests/test_v10_arms.py`: the `Broken` / `Missing` / `Short` cases (the real tree's `Broken` still replaces
   `one \`officeqa.py\` search`, a phrase that is not in the W0 prompt SEED)
8. delete `packs/officeqa/seed_notes.json`
9. `adapters/harness-w0/SEED.md` (the F8 phrase) - or regenerate: `python arms.py build`, then
   `pytest tests/test_v10_arms.py`
10. the index: `frameworks-v10/officeqa-tables-index/tables.sqlite` (otables-index-v10, built by task T, unchanged)
11. `W0-CONTENT.md`, `W0-BUILD-REPORT.md` and, once the parent has saved it, `w0replay/REPORT.md` (documentation)

Amend `officeqa-v10-analysis/synthesis/W0-PROPOSAL.md` §(a) (owner of the analysis folder): the harness bullet
"retry with fewer words and `--rows`" -> "retry with fewer words"; `SEED_AMENDMENTS` in the pack test then becomes
empty (the test asserts the amendment's old text occurs exactly once, so it will fail loudly the moment the proposal
is amended and must be emptied then).

## 4. README environment text block (W0-CONTENT §3, for `kit.readme_l0` and `campaign.HELPER_README`)

Append to the `# Environment` list of `kit.readme_l0` (after the corpus/index bullets; `{corpus}` is interpolated):

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

and the sentence after it by: "Search returns a dict whose `results` list holds ranked excerpts with filename and
line anchors (`start_line`..`end_line`); read returns numbered document lines. The helper contains no answers or
source hints. You may also read the files directly (rg, sed, Python)." (Result keys are those of
`officeqa_tools.OfficeQACorpus.search`: `filename`, `start_line`, `end_line`, `excerpt`, `rank`.) Tests to touch:
`tests/test_v10_kit.py::test_readme_l0_identical_for_every_arm` and any test pinning `HELPER_README` bytes.

## 5. Controls implementation note (W0-CONTENT §4, for `controller_runtime.py`)

`controls.json` values stay the design's: `{"loop_guard": true, "loop_threshold": 3, "loop_notes_max": 3,
"splice": true, "ledger_splice_bytes": 8000, "gate": false, "enforce_requery_max": 0}` plus the new knob below.

1. Remove the `self.noted` skip (`MechController._loop_guard`, the `if cmd in self.noted: ... continue` branch,
   ~573-577). `self.noted[cmd]` holds the count at the last note; when `count - self.noted[cmd] >= loop_threshold`,
   note it again (same `_loop_text`, `loop_notes += 1`, decision `loop_note`).
2. Hard block after the notes are used up, with a 20-line echo. Gate off, once `loop_notes >= loop_notes_max`, every
   further issue of a command whose (text, output sha256) pair repeats a previous call gets the block insert, not
   only the first (drop the once-per-command `self.blocked` guard, or key it by (cmd, sha) and reset it when the
   output changes). Message: `blocked: identical output already shown for: <cmd> (call N, k repeats)` + the first 20
   lines of that output + `LOOP_OPTIONS_NOGATE`. Cache the head when the output is ingested (`on_request`, per
   `function_call_output` item: `self.output_heads[(cmd, sha)]`, cap ~2,000 B) or add a `head` field to
   `ledger._output`. Update `HARD_BLOCK_TEMPLATE`/`fixed_templates` (their bytes are counted by
   `arms.harness_fixed_text` and `learner_common.controller_template_bytes`); `HARD_BLOCK_PREFIX` stays the prefix
   `summary_extra` counts as `hard_blocks`.
3. `empty_output_retry` knob (new `CONTROLS_DEFAULTS` key, default 1, range (0, 1); `checked_controls`,
   `arms.check_adapter` and PREREG record it). A manifest-tool command with exit 0 and an empty body is a capture
   loss: on the next request insert `[harness] <cmd> printed nothing (exit 0); output lost, run it again`, at most
   `empty_output_retry` times per command, exempt once from the loop threshold. Optional (5 capture losses, 0 grade
   effect in the pilot).
4. Unchanged: the gate-off loop templates, the ledger splice after compaction, the 8,000-byte page cap, T = 0, the
   3,600 s budget. The "Loops" advice sentence is gone from both SEEDs.

## 6. Tests

| suite | result |
|---|---|
| `tests/test_officeqa_tables.py` fast (small 12-issue index) | 31 passed (11 new `test_fix_*`), 29 skipped (slow) |
| `tests/test_officeqa_tables.py -m slow` (`V9_SLOW=1`, full index built in tmp, real corpus) | 29 passed, 49 s |
| `tests/test_officeqa_pack.py` | 68 passed |
| `tests/test_v10_arms.py` | 21 passed (was 4 failed) |
| `tests/test_v10_kit.py`, `tests/test_v10_gate.py` | 36 passed + 2 skipped, 12 passed |

Command: `cd officeqa-pro133-v10-w0work && adapter-comparison-work/.venv/bin/python -B -m pytest -q -p no:cacheprovider
tests/test_officeqa_tables.py tests/test_officeqa_pack.py tests/test_v10_arms.py` (add `V9_SLOW=1 -m slow` and
`OFFICEQA_CORPUS=<v9-p1-a corpus>` for the full-index tier).

## 7. Replay and harm summary (final tools; the full replay report is in the fix-pass StructuredOutput, to be saved as `w0replay/REPORT.md`)

2,290 unique pilot invocations (872 find_tables, 781 show_table, 637 printings) replayed through v9 tools + v9 index
and the final v10 tools + v10 index: exit transitions `0->0` 2,285 and `2->2` 5; **0 regressions, 0 crashes, 0
timeouts**; the first run's three `2->0` cases (`printings ... --all` accepted as an abbreviation) are gone. Right-cell
pages (1,053): find_tables pages where the v9 top group lost its parts to the budget 11 -> 2 (both are the W0-2
`Table N.` merge, whose merged group does print its parts); printings pages with a flipped SUMMARY verdict 7 -> 3
(two `Y?` weekly-row keys and one snapshot table keyed by as-of); the one v10 no-match (`--row "1939-Jan. 19"`, a day
that no row carries) now names the dated rows of that month; show_table unchanged (1 page loses one row to a window
shift, 61 pages gain NOTES/diagnosis lines). Leak scan of the masked samples: 0.

Harm tests (`w0replay/harm.py`, final tools): A - the W0-8 repair fires on none of the 8 right cells that saw the old
`spurious extra label` note (UID0112 b, UID0130 a, UID0154 b, UID0225 a/b2/c, UID0148 a/c) and fires on the positive
control OFS-2 2010_03:1869; B - every right-cell query keeps an allowed page rank (14 of 14 ok; UID0182's CM-I-1 at
rank 3); C - no token-fraction ranking, the AND and partial ranking keys are the proposal's.

## 8. Deviations from the proposal, with reasons

1. **Three reserved part blocks** (§1.4 says "parts are printed for groups in rank order until the budget is
   reached"). With group lines first, the pilot's `--all` pages left room for one block, and in 9 of 9 right-cell
   pages where the v9 target lost its parts it sat at v10 rank 2-3 (the W0-1 row-label scope puts larger
   row-label matches above it). Reserving the first three blocks (<= half the page) keeps those parts; the trailer
   still says how many group lines were cut. Ranking is untouched (§1.3 verdict, harm C).
2. **`[section]` entries collapse per heading** (W0-12 shows one line per section). A boilerplate title page or a
   recurring section name matched in 100+ issues would fill the block; one line per heading with the issue span and
   the latest location keeps the model's `--years` route and the budget. The pinned UID0215-type test still finds
   `treasury_bulletin_1988_03.txt:1688` with and without `--years`.
3. **Banner-variant merge in the SUMMARY** (W0-3 says "real OCR variants stay distinct; the page says 'also printed
   under panel'"). The replay showed that rule flips REVISED series to "same in all printings" (AY-1
   `series`/`aeries`, Derivation `basis`/`basic`/``); the merge applies only when no issue prints two stubs, so the
   stacked-panel cases the rule was written for (UID0007, UID0044, UID0211) stay split (test
   `test_fix_summary_merges_banner_variants_and_keeps_real_panels`).
4. **Day-level `--row` matches one day** (W0-11 spoke of month rows only). v9 matched the month prefix, listing
   every weekly row; the new rule lists the day, or names the dated rows when the day does not exist.
5. **SEED amendment F8** applied in `pack.py` while the proposal file is read-only for this tree: the drift test
   carries `SEED_AMENDMENTS` so the byte-for-byte pin stays auditable.
6. **F5 solved by reading the index header, not by capping siblings**: 72 ms vs 52 ms (v9) on the header-shift page
   instead of the review's 146 ms; the confirmation is display-only, the repair does not depend on it.
7. **F2 solved by grouping every matching part** (the review's first option); the OR-fallback path costs 0.85 s on
   its worst pilot flood (66,590 parts), 0.2 s on typical fallbacks.

## 9. Open items

- The proposal's §1.2 v9 rank numbers differ from the real v9 tool in 7 of 14 cases (measured v9 / proposal:
  UID0130 11/7, UID0224 1/10, UID0228 2/1, UID0019 3/5, UID0180 1/2, UID0220 2/1, UID0135 1/23); page-visible rank
  never worsened. `harm.py` prints both; the proposal's table should be corrected by its owner.
- 4 right-cell find_tables pages still lose the v9 top group entirely (W0-1 row-label scope: e.g. the single-issue
  group behind `find_tables "Ownership of United States Treasury Notes" --all` ranks below 25 of 378 groups). A
  title/header-first tie-break would fix them but contradicts §1.3 (it demotes UID0182's CM-I-1); left for the
  learner as a ranking-weight revision (W0-CONTENT §6).
- `w0replay` artefacts live in the session scratchpad (`w0replay3/`); regenerate with
  `python -B w0replay/collect.py && python -B w0replay/replay.py && python -B w0replay/harm.py` (~25 min, 4 workers).
- The README-environment block (§4) and the controls note (§5) are text for files this tree does not own
  (`kit.py`, `campaign.py`, `controller_runtime.py`), unchanged from W0-CONTENT.
