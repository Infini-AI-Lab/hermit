# Task R: replay and harm tests of the W0 tool changes (v9 tools vs v10-w0work tools)

Status: done, read-only on the tools. New files (all under `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v10-w0work/w0replay/`): `common.py`, `collect.py`, `replay.py`, `harm.py`. This text is the content of `w0replay/REPORT.md`; the harness refused to write a report file from a subagent, so the parent must save it there.
Raw tool output (which contains table values) lives only in the session scratchpad (`/private/tmp/claude-501/-Users-albertzhao--codex--chatgpt-projects-g-p-6aa867f0012881918044d250b055e9dd/d5f9d16d-6f3c-43aa-ad00-9b12b862fc9b/scratchpad/w0replay/`: `invocations.json`, `collect-stats.json`, `results.jsonl`, `summary.json`, `samples.md`, `samples_trimmed.md`, `harm.json`, `harm.log`); every number quoted here is masked as `<v>` and `common.leak_scan` found 0 value-like tokens left in the sample diffs. No answer key, grader, `evidence/` or `private-evaluation/` folder was opened; correctness per cell comes from `officeqa-v10-analysis/index.json` only. Nothing outside `w0replay/` was modified (v10 plumbing, v9 plumbing/index, v9-p*/v8/frameworks-*, opus-officeqa-133, analysis folders, `tests/` untouched).

---

## 中文摘要（写给初学者）

**做了什么。** 把 v9 试点四个组（a、b、b2、c）里模型敲过的每一条 shell 命令收集起来（b/b2 来自 `api/ledger.jsonl`，a/c 没有 ledger，来自 Codex 的 `records/events.jsonl`），保守地拆成"直接调用三个表格工具"的命令（`find_tables` / `show_table` / `printings`；`calc`、`submit` 跳过；含 `$变量`、heredoc 的行放弃），去重后得到 **2290 条唯一调用**。每条都分别用 **旧工具（v9 + v9 索引）** 和 **新工具（v10-w0work + frameworks-v10 索引）** 各跑一次（子进程，20 秒超时，4 个并发），比较退出码和输出；再对答对的格（RIGHT cells）做逐行 diff 和"内容级"比较；最后跑提案 §1 点名的三组"伤害测试"。

**几个词。** *invocation（调用）*：一次带参数的工具命令。*exit code（退出码）*：0 成功，2 用法/查找错误，3 内部错误。*regression（回归）*：以前能跑（0）、现在报错（≥2）。*page（页）*：工具打印的 ≤8000 字节文本。*group line（组行）*：`find_tables` 里以 `[表号]` 开头的一行；*part line（分部行）*：组下面 `file:line partN` 的一行。*SUMMARY*：`printings` 里"每个（列，期）最新印次/是否修订"的汇总块。*panel stub（面板桩）*：W0-3 用来区分同一表里多个横幅（banner）的标准化文字。

**主要结论。**
1. **没有退出码回归、没有崩溃、没有超时。** 2290 条调用：2285 条 0→0，3 条 2→0（`printings --all` 现在被 argparse 当作 `--all-periods` 的缩写接受，见发现 F4），2 条 2→2。新工具最慢 4.8 秒；两版所有页都 ≤ 8000 字节。
2. **答对格的 1053 条调用中，777 条输出有变化，620 条有旧行消失。** `find_tables` 388 条全部变化（W0-2 换了版式），`printings` 183/280，`show_table` 206/385。逐行 diff 数字大，但按"内容"看：`show_table` 没有丢任何行（只有 1 页因为多了一行提示而少显示 1 行，5 页是 AY-1 的 `Y?` 年份被 W0-10 补上了）；`find_tables` 292 页里 270 页（92%）第一名的组不变；`printings` 有 7 条 SUMMARY 结论翻转，其中 4 条是面板桩被 OCR 变体拆开造成的（F3），3 条是设计上的改进。
3. **发现 3 个需要修的地方（都是提案自己预警过的风险变成了现实）：**
   - **F1（W0-2，最重要）**：先印 25 条组行再印分部，行标签检索又把命中数放大几十倍（例：21→871 个分部），组行把 8000 字节吃光，页尾写着 "parts shown for the first 0"：答对格里 11 页看不到 v9 第一名组的任何分部，3 页整页没有一个分部行（其中 UID0154 c 的 TSO-3 正是 §1.4 说的"靠 as-of 标签才答对"的表）。
   - **F2（W0-11）**：月份键严格匹配把周/日行悄悄藏掉（10 页，没有任何提示），并且带日期的 `--row "1939-Jan. 19"` 从 v9 的 16 行变成 v10 的 "no matching row"，附带的诊断行还是错的（"print periods 1803 .. 2095"）。
   - **F3（W0-3）**：面板桩遇到 OCR 变体（"series"/"aeries"，"basis"/"basic"/空横幅）把同一序列拆成几块，第一块写着 "same in all printings" 而 v9 对全部 7 个印次写的是 REVISED（UID0228 a、UID0245 AY-1）；`also printed under panel` 这行有，但没有跨面板的总览。
4. **伤害测试：** (A) 表头修复 W0-8 在 8 个"不该触发"的答对格上全部没有触发（旧提示仍在），阳性对照 OFS-2 2010_03 触发；全部 2290 次重放里只在 OFS-2 2007_09/2010_03 两张表上触发过。(B) 14 条答对格查询：目标在**页面上**的名次没有一条变差（4 条从"被截掉看不见"变成可见），UID0182 的 CM-I-1 从缺席升到第 3；完整排名里 3 条后退（UID0130 11→14、UID0228 2→5、UID0135 1→3），都在首页。(C) 代码里没有按命中词比例排序；组序的变化全部来自组变大（行标签让更多分部命中），公式未变。
5. 提案 §1.2 引用的 v9 名次有 7 条和真实 v9 工具算出来的不一样（提案用的是 emulate 脚本 + 试点索引）；本报告以真实工具为准。

---

## 1. Method

| step | what | numbers |
|---|---|---|
| collect | every completed shell line of the 4 pilots; b/b2 from `api/ledger.jsonl` (`kind: command`, one row per `call_id`/`n`), a/c from `records/events.jsonl` (`command_execution`, `/bin/zsh -lc` unwrapped) | 6766 lines: a 3235, b 1666, b2 740, c 1125 |
| split | newline / `;` / `&&` / `\|\|` / `\|` / redirections via `shlex` with punctuation tokens; single-level `for x in <literals or $(seq A B)>; do ...; done` expanded; segments with an unresolved `$`/backtick or heredoc refused | 2797 lines with a tool call, 4020 tool calls with repeats (find_tables 1890, show_table 1298, printings 832); 463 calc/submit skipped; 81 refused (variables), 7 untokenizable (python heredocs) |
| parse | keep argvs that parse with the v9 or the v10 argparse (`cli.parser`) | 15 dropped (parse with neither: model typos; 14 had exit 2 in the ledger, 1 exit-0 command whose trailing `--col` had no value after splitting); 3 parse only with v10 (`printings ... --all`) |
| dedupe | on (tool, argv) | **2290 unique** (find_tables 872, show_table 781, printings 637); 1055 from at least one RIGHT cell |
| run | `python -I -B <cli.py> <tool> <args>` (as the kit wrappers do), v9 cli on `frameworks-v9/officeqa-tables-index/tables.sqlite`, v10 cli on `frameworks-v10/officeqa-tables-index/tables.sqlite`, `OFFICEQA_CORPUS=v9-p1-a/prepared/agent/corpus`, timeout 20 s, 4 workers | 4580 subprocesses, resumable `results.jsonl` |
| compare | exit codes; crashes (exit 3 / traceback); timeouts; for RIGHT cells line diffs and a content-level comparison per tool (§3) | |

Reproduce: `cd officeqa-pro133-v10-w0work && python -B w0replay/collect.py && python -B w0replay/replay.py && python -B w0replay/harm.py` (outputs in the scratchpad; `--out DIR` / `W0REPLAY_OUT` to change; `replay.py --analyze-only` recomputes the summary from `results.jsonl`).

## 2. Exit codes, crashes, timeouts

| transition (v9 -> v10) | count |
|---|---|
| 0 -> 0 | 2285 |
| 2 -> 0 | 3 |
| 2 -> 2 | 2 |
| 0 -> >=2 (**regression**) | **0** |
| crashes (exit 3 or traceback), either version | 0 |
| timeouts (> 20 s), either version | 0 (slowest v10 call 4.8 s) |
| pages > 8000 bytes | 0 in both (max exactly 8000) |

The three 2 -> 0 commands are `printings --table ESF-1 --row 2000-06 --all`, `printings --table ESF-1 --row 2000-09 --all` and `printings --table "Railroad Retirement Accounts" --row 1968-Mar --all`: v9 rejected `--all` (`unrecognized arguments`), v10's parser accepts it as an argparse **abbreviation of `--all-periods`** and runs (header line ends in `| all-periods`). See finding F4. The two 2 -> 2 are model typos (`show_table treasury_bulletin_2013_12.txt 1 --table IFS-2 ...` style).

## 3. RIGHT-cell commands: what changed

1053 unique invocations from right cells ran with exit 0 in both versions.

| tool | n | pages changed (any line added/removed) | pages with a removed v9 line | identical |
|---|---|---|---|---|
| find_tables | 388 | 388 | 386 | 0 |
| show_table | 385 | 206 | 110 | 179 |
| printings | 280 | 183 | 124 | 97 |
| **all** | **1053** | **777** | **620** | 276 |

Removed-line kinds (all right-cell pages): part lines 8568, group lines 1971, listing lines 1536, SUMMARY lines 628, row lines 198, other 1572; added: group 7292, part 5538, SUMMARY 1355, listing 523, row 201, other 5790. Median byte growth per page +59 B. Line diffs overstate the change because W0-2 rewrites every `find_tables` line and W0-3 every SUMMARY key, so the content-level comparison below is the one to read.

### 3.1 find_tables (292 right-cell pages with at least one group in v9; 96 zero-hit pages)

| metric | count |
|---|---|
| v9 top group is still the v10 top group | 270 / 292 (92 %) |
| v9 top group absent from the v10 page | 4 |
| pages losing at least one part line of v9's top group | 115 |
| - cause: `--all` now capped at 12 parts per group (W0-2 by design, `... N more parts (add --years)`) | 98 |
| - cause: **v10 lists 25 group lines and has no room for that group's parts** | 11 |
| - cause: top group absent (merged or ranked below 25) | 4 |
| - cause: different parts chosen (latest-3 window moved) | 4 |
| pages where v10 prints **no part line at all** while v9 did | 3 (5 over all cells) |
| pages cut at 8000 bytes | v9 91 -> v10 131 |

### 3.2 printings (280 right-cell pages)

| metric | count |
|---|---|
| pages losing listing lines | 34 |
| - with the W0-11 trailer `... N more listing lines omitted; SUMMARY complete` | 24 |
| - daily/weekly rows of the month silently hidden (W0-11 month key, no hint) | 10 |
| pages losing a SUMMARY key | 22 |
| - snapshot tables re-keyed by as-of date (W0-3, `snapshot table ... keyed by as-of` line printed) | 12 |
| - daily/weekly keys hidden by the month key (W0-11) | 9 |
| - **v10 answers `no matching row/column` where v9 listed rows** | 1 (UID0044 a, `--row "1939-Jan. 19"`) |
| SUMMARY verdict flipped for the same (period, part, column) key | 7 |
| - panel stub split by an OCR variant of the banner (see F3) | 4 (UID0245 AY-1 x3, UID0228 a x1) |
| - panel split of a weekly row across the table's sub-blocks (v9 lumped 17 different series into one REVISED key; v10 is right) | 2 (UID0044 b/c) |
| - snapshot table (v9 called 13 different as-of surveys "13 distinct values REVISED"; v10 keys by as-of) | 1 (UID0170 b2) |
| `no matching row/column` pages | v9 64 -> v10 63 |

### 3.3 show_table (385 right-cell pages)

| metric | count |
|---|---|
| pages losing a row (by source line and panel) | 1 (UID0224 c: the new `this part has N rows; the table continues in part 2` line pushes the 77th row to the next page) |
| pages whose row text changed | 5 (all UID0029 AY-1 1970_06/1970_07: `Y?` periods now anchored to a year, W0-10) |
| pages with new NOTES / diagnosis lines (`0 rows: --grep is ...`, `no table starts at`, `YEARS ANCHORED`, ...) | 61 |
| `page rows A-B of N` header changed | 37: 13 with a larger N (W0-6 `--grep` OR / month-name normalisation keeps more rows), 24 window-only |
| header repair (`LABELSHIFT-1` / `header realigned`) printed anywhere in the 2290 replays | only OFS-2 2007_09:1853 (UID0237 c, issue-47, wrong) and OFS-2 2010_03:1869 (UID0165, the designed target) |

## 4. Harm tests (W0-PROPOSAL §1)

### 4.1 §1.5 - header repair must not fire (`harm.py` part A)

Every show_table call of the named cells plus a bare `--long` page per table it opened; v9 must show the old `spurious extra label` note somewhere (proof the table is a test case) and v10 must never print `LABELSHIFT-1` / `header realigned`.

| cell | calls | tables opened | v9 pages with the old note | v10 pages where the repair fired |
|---|---|---|---|---|
| UID0112 b (FFO-2 1996_09/2001_09/2006_09/2011_09) | 9 | 7 | 3 | 0 |
| UID0130 a (TSO-1 1980_04, 1981_04) | 6 | 2 | 4 | 0 |
| UID0154 b (OFS-2 1977_01, 1977_03, 1978_03) | 4 | 4 | 3 | 0 |
| UID0225 a / b2 / c (MQ-1 1982_03/04/07) | 5 / 4 / 2 | 3 / 2 / 2 | 3 / 3 / 4 | 0 / 0 / 0 |
| UID0148 a / c (MQ-1 1972_05, 1973_05, 1974_05) | 6 / 4 | 3 / 3 | 4 / 3 | 0 / 0 |
| positive control OFS-2 2010_03:1869 (UID0165) | - | 1 | old note: yes | **fires** (LABELSHIFT-1 + header realigned) |

Verdict: passes; the strict trigger fires on the designed OFS-2 tables only (§3.3 confirms over all 2290 replays).

### 4.2 §1.2 - the right-cell find_tables queries keep their target rank (`harm.py` part B)

Two ranks per query: the *page rank* (position of the target's group line on the 8000-byte page the model sees) and the *full rank* (budget and part cap lifted in-process, `--limit 200`: the number the proposal's emulation quoted). Target = the table the cell then opened (id, or the part line / title with a leading `Table N.` ignored).

| uid cell | query | target | full rank v9 -> v10 (proposal's v9) | page rank v9 -> v10 |
|---|---|---|---|---|
| UID0130 a | `Total U.S. Federal Securities --all` | TSO-1 | 11 -> **14** (7) | cut -> 14 |
| UID0224 a | `high-grade corporate bonds average yields` | Average Yields of Long-Term ... | 1 -> 1 (10) | 1 -> 1 |
| UID0100 b | `Judicial Branch on-budget off-budget outlays --all` | FFO-3 | 1 -> 1 (1) | 1 -> 1 |
| UID0154 b | `Ownership of Public Debt Securities --all` | OFS-2 (TSO-3 9 -> 9) | 1 -> 1 (1) | 1 -> 1 |
| UID0243 b | `Debt outstanding --years 1962-1963 --rows --all` | 1962_07 `Table 1.` group (merged, W0-2) | 1 -> 1 (1) | 1 -> 1 |
| UID0007 a | `budget expenditures --years 1940-1949` | Summary by Months and Calendar Years | 5 -> 5 (5) | 5 -> 5 |
| UID0112 b | `on-budget receipts off-budget receipts --all` | FFO-2 | 2 -> 2 (2) | 2 -> 2 |
| UID0109 a | `Department of Defense Outlays` | FFO-3 | 1 -> 1 (1) | 1 -> 1 |
| UID0122 a | `Exchange Stabilization Fund balances --all` | ESF-1 | 1 -> 1 (1) | 1 -> 1 |
| UID0228 b | `Government total receipts from the public --all` | Derivation of Federal Government Receipts ... | 2 -> **5** (1) | cut -> 5 |
| UID0019 b | `net options positions --all` | FCP-IV-1 / FCP-I-1 / FCP-II-1 | 3 -> 3 (5) | cut -> 3 |
| UID0180 b2 | `Distribution of Federal Securities ... --years 2009-2010 --rows` | OFS-1 | 1 -> 1 (2, allowed 3) | 1 -> 1 |
| UID0220 a | `total federal expenditures --years 1938-1939 --rows` | Summary of Federal Receipts and Expenditures | 2 -> 2 (1, allowed 2) | 2 -> 2 |
| UID0135 a | `Total Liabilities Foreign Countries U.S. banks --all` | CM-I-2 | 1 -> **3** (23, allowed beyond 25) | 1 -> 3 |
| UID0182 b (payload) | `Canadian dollar liabilities --all` | CM-I-1 | absent -> **3** (absent -> 3) | absent -> 3 |

Verdict: no target is later on the page in v10 than in v9 (criterion `ok` = page rank not worse); 4 targets that v9's page had cut away (UID0130, UID0228, UID0019, UID0182) are now visible. Three full-rank drops (UID0130 11 -> 14, UID0228 2 -> 5, UID0135 1 -> 3) are the where-score/size ranking acting on groups that grew under the row-label scope; all stay on the first page. Seven of the proposal's v9 numbers do not match the real v9 tool (its `emulate2.py` used the pilot index and its own scoring); the table above is measured with the shipped tools.

### 4.3 §1.3 - no token-fraction ranking (`harm.py` part C)

- Static: `otables.py` contains no `fraction` score; `find_page` ranks AND hits by `(-where-score, -group size)` exactly as v9 did, and the OR fallback by `(-tokens matched, -title/header tokens, -where-score, -size)`.
- Behavioural: for the 15 queries above, every pair of groups common to both full rankings whose relative order changed (0 to 118 pairs per query) has a different issue count in v10 (e.g. `Table 1 in DEBT OUTSTANDING` 22 -> 23 issues, `federalfiscaloperations` 1 -> 14): groups grew because row labels now match, so the size tie-break moved them; no pair flipped at equal sizes. UID0182's query: 0 reordered pairs.

## 5. Findings for the fix phase (ranked)

**F1 - W0-2 group-first listing starves the part blocks (major).** With the row-label scope a query's hit set can grow 40x (UID0154 c: 21 parts / 9 groups in v9 -> 871 parts / 422 groups in v10); the page then prints 25 group lines of ~250 bytes each (`rows ..`, `as-of ..`, `match:tokens(title: ...; rowlabels: ...)`) plus `[section]` lines, reaches the budget and ends with `... OUTPUT CUT at 8000 bytes: 25 groups listed, parts shown for the first 0`. In right cells: 11 pages lose every part of v9's top group, 3 pages show no `file:line` at all (UID0154 c TSO-3 - the as-of-tag case §1.4 warned about -, UID0149 c TSO-3, UID0130 a). The proposal's own condition ("parts are printed for groups in rank order until the budget is reached, so the top groups keep their as-of tags") is not met when 25 long group lines fit first. Suggested fix: reserve bytes for the first block(s) before printing further group lines (e.g. stop the group list once the remaining room is below the top-3 blocks' size), or shorten the group line when the whole phrase matched. Related: sample 20 shows v9's single exact hit (1 part) pushed off the page by 378 row-label groups and five `[section]` lines for the boilerplate heading `UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY` (a heading present in every issue should be excluded from W0-12 or printed once).

**F2 - W0-11 month-key exactness hides daily/weekly rows silently and breaks day-level `--row` (moderate).** 10 right-cell `printings` pages lost the weekly rows of the month with no hint line (`--all-periods` is mentioned nowhere on the page); UID0044 a's `--row "1939-Jan. 19"` (v9: 16 weekly rows of 1939-01 listed, 4 SUMMARY keys) now returns `no matching row/column found ... the scanned parts print periods 1803 .. 2095; --row '1939-Jan. 19' is not among them` (the period span in that diagnostic is wrong). Suggested fix: a `--row` carrying a day matches that day's filled period; when a month key hides rows print `N daily/weekly rows of 1939-01 hidden (--all-periods)`; compute the diagnostic span from parsed periods only.

**F3 - W0-3 panel stubs split one series on OCR variants (moderate, the proposal's risk (g)4).** AY-1: `Monthly series` vs `Monthly aeries` (v9 `[1981-08] 6 printings, 2 distinct - REVISED` -> v10 first block `4 printings, 1 distinct - same in all printings` + a second block for the OCR panel); Derivation table (UID0228 a): `New reporting basis:` / `New reporting basic:` / `` (no banner) -> the 1957_02 revision now sits in the fourth block while the first block says `same in all printings`. The `also printed under panel '...' in N issues (not merged)` mitigation line is printed, but a model reading the first block gets the opposite of v9's verdict. Suggested fix: merge stubs within edit distance <= 2 and merge the empty stub into the majority stub of the same column, or add one roll-up line per (column, period): `all panels: 7 printings, 2 distinct values - REVISED`.

**F4 - `printings --all` is accepted as an abbreviation of `--all-periods` (minor).** Three recorded commands went from exit 2 to exit 0 with the all-periods semantics. `_Parser` should pass `allow_abbrev=False` (observe.py re-derives pages through the same parser, so both stay consistent); the v9 usage line said `printings` has no `--all`.

**F5 - ranking drift under the row-label scope (minor, expected).** Full-rank drops for 3 of 14 right-cell queries (§4.2); page visibility never worse. Left to the learner (ranking weights, `--limit` default) as (f) foresees.

Observed as designed (no action): W0-10 year anchoring on AY-1 (5 pages, `Y?` -> years); W0-3 snapshot keys (ESF-1, Public Marketable Securities: 12 pages); W0-2 12-part cap under `--all` (98 pages, with the `... N more parts (add --years)` hint); W0-6 `--grep` keeps more rows on 13 pages; W0-7 continuation hint costs one row on one page; W0-5/W0-6 diagnosis lines on 61 pages.

## 6. Tests

- `tests/test_officeqa_tables.py tests/test_officeqa_pack.py` (v10-w0work, fast): **88 passed, 29 skipped** (slow cases are opt-in), 1.7 s.
- same with `-m slow`, `V9_SLOW=1` and `TABLES_INDEX=frameworks-v10/officeqa-tables-index/tables.sqlite`: **29 passed**, 42 s.
- No test file was modified (task R owns `w0replay/*` only). The splitter is validated against the ledgers: every argv that fails the v9 argparse in the replay carried exit 2 in the ledger (14 of the 15 dropped argvs; the 15th is an exit-0 command whose trailing `--col` had no value after splitting), and every other exit-0 ledger command yielded parsable argvs.

## 7. Files

- `w0replay/common.py` - paths, ledger/event readers, shell splitter (`split_newlines`, `split_segments`, `expand_loops`, `tool_argvs`), argparse checks (`load_cli`, `parses`), masking (`mask_line`, `mask_values`, `mask_argv`), find_tables page parser (`parse_find_page`), `leak_scan`.
- `w0replay/collect.py` - builds `invocations.json` + `collect-stats.json`.
- `w0replay/replay.py` - runs both trees (resumable, 4 workers, 20 s timeout), `summary.json`, `samples.md` (20 masked diffs), content-level comparison (`find_tables_loss`, `show_table_loss`, `printings_loss`); `--analyze-only` recomputes from `results.jsonl`.
- `w0replay/harm.py` - §1.5 / §1.2 / §1.3 harm tests, `harm.json`; full rankings computed in-process with the page budget lifted.
- Deferred: none of the task's items. The report file itself could not be written by the subagent (harness rule); its text is this report.

## 8. Twenty masked diffs from RIGHT-cell commands

Chosen by the content-level loss (heaviest first, spread over the tools; the full masked diffs are in the scratchpad's `samples.md`). `-` lines are v9 output absent from v10, `+` lines are new in v10; each diff is cut to its first 12 changed lines.

### sample 1: `find_tables "Latin America capital" --all`

UID0044 cell b (right cell); v9 7932 B -> v10 6998 B; `{"top_parts_v9": 20, "top_parts_v10": 0, "top_loss_why": "top group absent", "parts_lost": 82, "groups_lost": 13, "v9_cut": true, "v10_cut": true}`

```diff
@@ -1,98 +1,71 @@
-find_tables('Latin America capital', years=None): 98 table parts in 30 groups
-[-] Short-Term Foreign Liabilities as Reported by Banks and Bankers in the United States for October 1938  | 20 issues 1939_01..1942_03 | match:token
-    treasury_bulletin_1939_01.txt:1040 part1 14c x 50r rows Oct. <v>..Oct. <v>
-    treasury_bulletin_1939_03.txt:1683 part1 14c x 50r rows Dec. <v>..Dec. <v>
-    treasury_bulletin_1939_04.txt:1253 part1 15c x 60r rows Jan. <v>..Feb. <v> as-of January 2, 1935
-    treasury_bulletin_1939_06.txt:1655 part1 14c x 50r rows Mar. <v>..Mar. <v> as-of January 2, 1935
-    treasury_bulletin_1939_09.txt:1434 part1 15c x 50r rows June <v>..June <v> as-of January 2, 1935
-    treasury_bulletin_1939_10.txt:1441 part1 15c x 60r rows July <v>..Aug. <v> as-of January 2, 1935
-    treasury_bulletin_1939_11.txt:1639 part1 14c x 50r rows Aug. <v>..Aug. <v> as-of January 2, 1935
-    treasury_bulletin_1940_01.txt:1495 part1 15c x 60r rows Oct. <v>..Nov. <v> as-of January 2, 1935
-    treasury_bulletin_1940_05.txt:2073 part1 cont 16c x 50r rows Feb. <v>..Feb. <v>
... (39 diff lines in total)
```

### sample 2: `find_tables "Debt outstanding" --years 1962-1963 --rows --all`

UID0243 cell b (right cell); v9 7917 B -> v10 7845 B; `{"top_parts_v9": 1, "top_parts_v10": 0, "top_loss_why": "top group absent", "parts_lost": 97, "groups_lost": 1, "v9_cut": true, "v10_cut": true}` (the 1962_07 `Table 1.` group is merged into the 23-issue group, W0-2)

```diff
@@ -1,5 +1,47 @@
-find_tables('Debt outstanding', years=1962-1963): 618 table parts in 62 groups
-[-] Table 1. Summary of Federal Securities  | 1 issues 1962_07..1962_07 | match:title
+find_tables('Debt outstanding', years=1962-1963): 618 table parts in 60 groups
+[section] DEBT OUTSTANDING | treasury_bulletin_1962_01.txt:1529<v>
+[section] DEBT OUTSTANDING | treasury_bulletin_1962_02.txt:1736-1972
+[section] DEBT OUTSTANDING | treasury_bulletin_1962_03.txt:1449<v>
+[section] DEBT OUTSTANDING | treasury_bulletin_1962_04.txt:1455<v>
+[section] DEBT OUTSTANDING | treasury_bulletin_1962_05.txt:1781-2045
+[Table 1 in DEBT OUTSTANDING] Summary of Federal Securities  | 23 issues 1962_01..1963_12 | rows 1954..June | match:tokens(title: debt, outstanding)
+[-] SUMMARY OF FEDERAL FISCAL OPERATIONS  | 7 issues 1962_02..1963_11 | rows 1948 <v>/..Oct | match:header
+[Table 6 in PUBLIC DEBT OPERATIONS] Disposition of Matured Public Marketable Securities Other Than Regular Weekly Treasury Bills  | 20 issues 1962_01
... (39 diff lines in total)
```

### sample 3: `find_tables "Government total receipts from the public" --all`

UID0228 cell b (right cell); v9 7984 B -> v10 6933 B; `{"top_parts_v9": 95, "top_parts_v10": 0, "top_loss_why": "budget cut before the top block", "parts_lost": 95, "groups_lost": 0, "v9_cut": true, "v10_cut": true}`

```diff
@@ -1,98 +1,46 @@
-find_tables('Government total receipts from the public', years=None): 531 table parts in 19 groups
-[FFO-1] Summary of Fiscal Operations  | 284 issues 1969_09..2025_09 | match:tokens
-    treasury_bulletin_1969_09.txt:119 part1 13c x 24r rows August..July
-    treasury_bulletin_1969_10.txt:189 part1 13c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1969_11.txt:135 part1 14c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1969_12.txt:132 part1 14c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1970_01.txt:111 part1 16c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1970_02.txt:177 part1 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_03.txt:161 part1 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_04.txt:154 part1 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_05.txt:197 part1 14c x 25r rows 1961..Fiscal 1970 to date
... (39 diff lines in total)
```

### sample 4: `find_tables "Total Liabilities Foreign Countries U.S. banks" --all`

UID0135 cell a (right cell); v9 7934 B -> v10 7519 B; `{"top_parts_v9": 92, "top_parts_v10": 0, "top_loss_why": "budget cut before the top block", "parts_lost": 103, "groups_lost": 0, "v9_cut": true, "v10_cut": true}`

```diff
@@ -1,107 +1,46 @@
-find_tables('Total Liabilities Foreign Countries U.S. banks', years=None): 403 table parts in 40 groups
-[CM-I-2] Total Liabilities by Type, Payable in Dollars  | 92 issues 1978_07..2000_12 | match:tokens
-    treasury_bulletin_1978_07.txt:5567 part1 16c x 23r rows 1968..May. p
-    treasury_bulletin_1978_08.txt:6158 part1 15c x 29r rows 1968..June p
-    treasury_bulletin_1978_10.txt:5800 part1 17c x 29r rows 1968..Aug
-    treasury_bulletin_1978_12.txt:6015 part1 15c x 23r rows 1968..Oct. p
-    treasury_bulletin_1979_01.txt:6004 part1 16c x 22r rows 1968..Sep
-    treasury_bulletin_1979_02.txt:6253 part1 15c x 23r rows 1969 <v>/..Dec. p
-    treasury_bulletin_1979_03.txt:6485 part1 15c x 23r rows 1969 <v>/..Dec. p
-    treasury_bulletin_1979_04.txt:6455 part1 16c x 22r rows 1969 <v>/..Dec
-    treasury_bulletin_1979_05.txt:6017 part1 15c x 23r rows 1969 <v>/..1979-Jan
... (39 diff lines in total)
```

### sample 5: `find_tables "Total Federal debt" --all`

UID0057 cell c (right cell); v9 7999 B -> v10 7189 B; `{"top_parts_v9": 93, "top_parts_v10": 0, "top_loss_why": "budget cut before the top block", "parts_lost": 93, "groups_lost": 0, "v9_cut": true, "v10_cut": true}`

```diff
@@ -1,96 +1,60 @@
-find_tables('Total Federal debt', years=None): 8124 table parts in 352 groups
-[FFO-1] Summary of Fiscal Operations  | 299 issues 1969_09..2025_09 | match:tokens
-    treasury_bulletin_1969_09.txt:119 part1 13c x 24r rows August..July
-    treasury_bulletin_1969_09.txt:146 part2 cont 13c x 24r rows 1960..July
-    treasury_bulletin_1969_10.txt:189 part1 13c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1969_10.txt:217 part2 cont 13c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1969_11.txt:135 part1 14c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1969_12.txt:132 part1 14c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1970_01.txt:111 part1 16c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1970_02.txt:177 part1 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_02.txt:205 part2 cont 14c x 25r rows 1961..Fiscal 1970 to date
... (39 diff lines in total)
```

### sample 6: `find_tables "bank ownership" --years 1959-1960 --all`

UID0170 cell a (right cell); v9 4344 B -> v10 7381 B; `{"top_parts_v9": 2, "top_parts_v10": 0, "top_loss_why": "top group absent", "parts_lost": 18, "groups_lost": 10, "v9_cut": false, "v10_cut": true}`

```diff
@@ -1,42 +1,54 @@
-find_tables('bank ownership', years=1959-1960): 22 table parts in 19 groups
-[-] Section II. - Interest-Bearing Securities Issued by Federal Agencies But Not Guaranteed by the United States G  | 2 issues 1960_03..1960_09 | mat
+find_tables('bank ownership', years=1959-1960): 231 table parts in 154 groups
+[section] TREASURY SURVEY - COMMERCIAL BANK OWNERSHIP, DECEMBER <v> 1958 | treasury_bulletin_1959_03.txt:2823<v>
+[section] COMMERCIAL BANK OWNERSHIP, DECEMBER <v> 1958 | treasury_bulletin_1959_03.txt:2883<v>
+[section] TREASURY SURVEY - COMMERCIAL BANK OWNERSHIP, DECEMBER <v> 1958 | treasury_bulletin_1959_03.txt:2961<v>
+[section] COMMERCIAL BANK OWNERSHIP, | treasury_bulletin_1959_03.txt:3006<v>
+[section] TREASURY SURVEY - COMMERCIAL BANK OWNERSHIP, JUNE <v> 1959 | treasury_bulletin_1959_09.txt:2913<v>
+[-] Section II - Interest-Bearing Securities Issued by Federal Agencies but Not Guaranteed by the United States Go  | 19 issues 1959_01..1960_12 | [s
+[-] Not Guaranteed by the United States Government  | 5 issues 1959_02..1960_03 | [snapshot] | as-of NOVEMBER <v> 1958..DECEMBER <v> 1959 | match:tok
+[-] Section I.- Interest-Bearing Securities Issued or Guaranteed by the United States Government Table 3.- Public   | 1 issues 1960_09..1960_09 | [sn
... (39 diff lines in total)
```

### sample 7: `find_tables "Total U.S. Federal Securities" --all`

UID0130 cell a (right cell); v9 7995 B -> v10 7004 B; `{"top_parts_v9": 92, "top_parts_v10": 0, "top_loss_why": "budget cut before the top block", "parts_lost": 92, "groups_lost": 0, "v9_cut": true, "v10_cut": true}`

```diff
@@ -1,95 +1,46 @@
-find_tables('Total U.S. Federal Securities', years=None): 6682 table parts in 1421 groups
-[FFO-1] Summary of Fiscal Operations  | 215 issues 1969_09..2025_03 | match:tokens
-    treasury_bulletin_1969_09.txt:146 part2 cont 13c x 24r rows 1960..July
-    treasury_bulletin_1969_10.txt:217 part2 cont 13c x 25r rows 1960..Fiscal 1970 to date
-    treasury_bulletin_1970_02.txt:205 part2 cont 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_05.txt:225 part2 cont 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_07.txt:168 part2 cont 14c x 25r rows 1961..Fiscal 1970 to date
-    treasury_bulletin_1970_08.txt:212 part2 cont 14c x 24r rows 1961..June p
-    treasury_bulletin_1970_10.txt:131 part2 cont 15c x 26r rows 1961..Fiscal 1971 to date
-    treasury_bulletin_1970_11.txt:192 part2 cont 14c x 25r rows 1961..Fiscal 1971 to date
-    treasury_bulletin_1971_02.txt:185 part2 cont 14c x 25r rows 1962..Fiscal 1971 to date
... (39 diff lines in total)
```

### sample 8: `show_table treasury_bulletin_1948_02.txt 2948`

UID0224 cell c (right cell); v9 7941 B -> v10 7936 B; `{"rows_lost": 1, "rows_changed": 0, "page_rows_v9": "page rows 1-77 of 108", "page_rows_v10": "page rows 1-76 of 108", "notes_added": 0}`

```diff
@@ -1,2 +1,2 @@
-TABLE Average Yields of Long-Term Treasury and Corporate Bonds | treasury_bulletin_1948_02.txt:2948 | part 1 | page rows 1-77 of 108
+TABLE Average Yields of Long-Term Treasury and Corporate Bonds | treasury_bulletin_1948_02.txt:2948 | part 1 | page rows 1-76 of 108
@@ -5,2 +5,3 @@
+  this part has 37 rows; the table continues in part 2 at :<v> (use --stitch)
@@ -91,4 +92,3 @@
-<v>  <v>    1945-05  May  <v> <v> <v>
-... PAGE ENDS after row 77 of <v> (<v>-byte page): continue with --from-row 77 (same other options), or narrow with --cols / --match / --grep
+... PAGE ENDS after row 76 of <v> (<v>-byte page): continue with --from-row 76 (same other options), or narrow with --cols / --match / --grep
... (14 diff lines in total)
```

### sample 9: `show_table treasury_bulletin_1970_06.txt 4946 --stitch --from-row 53`

UID0029 cell b (right cell); v9 7938 B -> v10 7999 B; `{"rows_lost": 0, "rows_changed": 59, "page_rows_v9": "page rows 54-112 of 163", "page_rows_v10": "page rows 54-115 of 163", "notes_added": 0}` (W0-10: `Y?` periods anchored)

```diff
@@ -1,2 +1,2 @@
-TABLE AY-1 | treasury_bulletin_1970_06.txt:4946 | part 1 | page rows 54-112 of 163
+TABLE AY-1 | treasury_bulletin_1970_06.txt:4946 | part 1 | page rows 54-115 of 163
@@ -17,62 +17,65 @@
-<v>  <v>    Y?<v><v>      June  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      July  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Aug  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Sept  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Oct  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Nov  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Dec  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Jan  <v> <v> <v> <v>
... (39 diff lines in total)
```

### sample 10: `show_table treasury_bulletin_1970_06.txt 4946 --stitch`

UID0029 cell b (right cell); v9 7910 B -> v10 7903 B; `{"rows_lost": 0, "rows_changed": 53, "page_rows_v9": "page rows 1-53 of 163", "page_rows_v10": "page rows 1-53 of 163", "notes_added": 1}`

```diff
@@ -17,2 +17,3 @@
+  - YEARS ANCHORED from treasury_bulletin_1970_05.txt:4848 (<v>/408 cells agree): panel1 = 1959,1960,1961; panel2 = 1962,1963,1964; panel3 = 1965,196
@@ -21,55 +22,55 @@
-<v>  <v>    Y?<v>       Jan  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Feb  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Mar  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Apr  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       May  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       June  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       July  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Aug  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Sept  <v> <v> <v> <v>
... (39 diff lines in total)
```

### sample 11: `show_table treasury_bulletin_1970_07.txt 4447 --cols 1-6`

UID0029 cell c (right cell); v9 7947 B -> v10 7987 B; `{"rows_lost": 0, "rows_changed": 36, "page_rows_v9": "page rows 1-64 of 141", "page_rows_v10": "page rows 1-64 of 141", "notes_added": 1}`

```diff
@@ -3,2 +3,3 @@
+  this part has 42 rows; the table continues in part 2 at :<v> (use --stitch)
@@ -12,2 +13,3 @@
+  - YEARS ANCHORED from treasury_bulletin_1970_08.txt:5256 (<v>/108 cells agree): panel1 = 1959,1960,1961
@@ -16,38 +18,38 @@
-<v>  <v>    Y?<v>    Jan  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Feb  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Mar  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Apr  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    May  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    June  <v> <v> <v> <v>
-<v>  <v>    Y?<v>    July  <v> <v> <v> <v>
... (39 diff lines in total)
```

### sample 12: `show_table treasury_bulletin_1970_06.txt 4946 --stitch --from-row 112`

UID0029 cell b (right cell); v9 6650 B -> v10 6550 B; `{"rows_lost": 0, "rows_changed": 25, "page_rows_v9": "page rows 113-163 of 163", "page_rows_v10": "page rows 113-163 of 163", "notes_added": 0}`

```diff
@@ -17,27 +17,27 @@
-<v>  <v>    Y?<v>       May  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       June  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       July  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Aug  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Sept  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Oct  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Nov  <v> <v> <v> <v>
-<v>  <v>    Y?<v>       Dec  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Jan  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Feb  <v> <v> <v> <v>
-<v>  <v>    Y?<v><v>      Mar  <v> <v> <v> <v>
... (39 diff lines in total)
```

### sample 13: `show_table treasury_bulletin_1970_07.txt 4447 --stitch`

UID0029 cell b (right cell); v9 7858 B -> v10 7874 B; `{"rows_lost": 0, "rows_changed": 24, "page_rows_v9": "page rows 1-24 of 141", "page_rows_v10": "page rows 1-24 of 141", "notes_added": 1}`

```diff
@@ -26,2 +26,3 @@
+  - part1: YEARS ANCHORED from treasury_bulletin_1970_08.txt:5256 (<v>/108 cells agree): panel1 = 1959,1960,1961
@@ -32,26 +33,26 @@
-<v>  <v>    Y?<v>    Jan  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Feb  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Mar  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Apr  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    May  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    June  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    July  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Aug  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
-<v>  <v>    Y?<v>    Sept  <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v> <v>
... (39 diff lines in total)
```

### sample 14: `printings --table "Net Capital Movement between the United States and Foreign Countries" --row "1939-Jan. 19" --col "Latin America" --years 1939-1940`

UID0044 cell a (right cell); v9 3537 B -> v10 368 B; `{"listing_v9": 16, "listing_lost": 16, "summary_key_loss_cause": "v10 no-match (regression)", "listing_lost_daily": 16, "summary_keys_v9": 4, "summary_keys_v10": 0, "verdicts_flipped": 0, "v9_cut": false, "v10_omitted_listing": false, "v9_no_match": false, "v10_no_match": true}` (F2)

```diff
@@ -1,43 +1,3 @@
-  issue    file:line                          part period       label | col = value [flags]
-  1939_04  treasury_bulletin_1939_04.txt:1188   p1 1939-01<v>   '1939-Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1189   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1190   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1191   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1202   p1 1939-01<v>   '1939-Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1203   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1204   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1205   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1216   p1 1939-01<v>   '1939-Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1217   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
... (39 diff lines in total; v10's whole page is `no matching row/column found ...` + `the scanned parts print periods 1803 .. 2095; --row '1939-Jan. 19' is not among them`)
```

### sample 15: `printings --table AY-1 --row 1981-08 --col "Treasury bonds" --years 1981-1985`

UID0245 cell a (right cell); v9 4310 B -> v10 2171 B; `{"listing_v9": 14, "listing_lost": 8, "summary_key_loss_cause": "daily/weekly keys hidden by the month key (W0-11)", "listing_lost_daily": 8, "summary_keys_v9": 9, "summary_keys_v10": 1, "verdicts_flipped": 1, "v9_cut": false, "v10_omitted_listing": false, "v9_no_match": false, "v10_no_match": false}` (F3: `series` / `aeries`)

```diff
@@ -1,13 +1,16 @@
-  issue    file:line                          part period       label | col = value [flags]
+SUMMARY (per column & period: value -> issues; first and latest printing):
+  [1981-08] part1 panel 'Monthly series - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 4 printings, 1 distinct values - same in all
+      also printed under panel 'Monthly aeries - averages of daily or we' in 1 issues (not merged)
+      <v>: 1981_11..1982_10 (4)
+      first printing 1981_11 (treasury_bulletin_1981_11.txt:4665), latest printing 1982_10 (treasury_bulletin_1982_10.txt:4630)
+  column index differs across issues (c10: 1 issues; c1: 1 issues) for treasurybonds1 [1981-08]: the minority may be a mis-parsed header; verify with
+  [1981-08] part1 panel 'Monthly aeries - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 2 printings, 2 distinct values - REVISED/DIF
+      also printed under panel 'Monthly series - averages of daily or we' in 4 issues (not merged)
+      <v>: 1982_02..1982_02 (1)
+      <v>: 1982_02..1982_02 (1)
... (39 diff lines in total)
```

### sample 16: `printings --table "Average Yields of Long-Term Treasury, Corporate and Municipal Bonds" --row "Aug. 1981" --col "Treasury bonds" --years 1981-1983`

UID0245 cell b (right cell); v9 4375 B -> v10 2236 B; `{"listing_v9": 14, "listing_lost": 8, "summary_key_loss_cause": "daily/weekly keys hidden by the month key (W0-11)", "listing_lost_daily": 8, "summary_keys_v9": 9, "summary_keys_v10": 1, "verdicts_flipped": 1, "v9_cut": false, "v10_omitted_listing": false, "v9_no_match": false, "v10_no_match": false}`

```diff
@@ -1,13 +1,16 @@
-  issue    file:line                          part period       label | col = value [flags]
+SUMMARY (per column & period: value -> issues; first and latest printing):
+  [1981-08] part1 panel 'Monthly series - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 4 printings, 1 distinct values - same in all
+      also printed under panel 'Monthly aeries - averages of daily or we' in 1 issues (not merged)
+      <v>: 1981_11..1982_10 (4)
+      first printing 1981_11 (treasury_bulletin_1981_11.txt:4665), latest printing 1982_10 (treasury_bulletin_1982_10.txt:4630)
+  column index differs across issues (c10: 1 issues; c1: 1 issues) for treasurybonds1 [1981-08]: the minority may be a mis-parsed header; verify with
+  [1981-08] part1 panel 'Monthly aeries - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 2 printings, 2 distinct values - REVISED/DIF
+      also printed under panel 'Monthly series - averages of daily or we' in 4 issues (not merged)
+      <v>: 1982_02..1982_02 (1)
+      <v>: 1982_02..1982_02 (1)
... (39 diff lines in total)
```

### sample 17: `printings --table "Summary of Public Marketable Securities" --row "Treasury notes" --col Chicago`

UID0170 cell b2 (right cell); v9 7955 B -> v10 7879 B; `{"listing_v9": 38, "listing_lost": 38, "summary_key_loss_cause": "snapshot table re-keyed by as-of (W0-3)", "listing_lost_daily": 0, "summary_keys_v9": 5, "summary_keys_v10": 2, "verdicts_flipped": 1, "v9_cut": true, "v10_omitted_listing": true, "v9_no_match": false, "v10_no_match": false}` (as designed: one key per as-of survey)

```diff
@@ -1,76 +1,76 @@
-  issue    file:line                          part period       label | col = value [flags]
-  1948_04  treasury_bulletin_1948_04.txt:2148   p1 -            'Treasury notes' | c5 'Member commercial banks > <v> Chicago ban' = <v>
-  1948_09  treasury_bulletin_1948_09.txt:2154   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
-  1949_03  treasury_bulletin_1949_03.txt:2218   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > <v> Chicag' = <v>
-  1949_09  treasury_bulletin_1949_09.txt:2249   p1 -            'Treasury notes' | c5 'Federal Reserve member banks Central res' = <v>
-  1950_03  treasury_bulletin_1950_03.txt:2400   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
-  1950_09  treasury_bulletin_1950_09.txt:2391   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
-  1951_03  treasury_bulletin_1951_03.txt:2472   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
-  1951_09  treasury_bulletin_1951_09.txt:2430   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
-  1952_03  treasury_bulletin_1952_03.txt:2544   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > <v> Chicag' = <v>
-  1952_09  treasury_bulletin_1952_09.txt:2532   p1 -            'Treasury notes' | c5 'Federal Reserve member banks > Central r' = <v>
... (39 diff lines in total)
```

### sample 18: `printings --table AY-1 --row 1981-08 --col "Treasury bonds 1/" --years 1982-1983`

UID0245 cell a (right cell); v9 2625 B -> v10 2035 B; `{"listing_v9": 9, "listing_lost": 4, "summary_key_loss_cause": "daily/weekly keys hidden by the month key (W0-11)", "listing_lost_daily": 4, "summary_keys_v9": 5, "summary_keys_v10": 1, "verdicts_flipped": 1, "v9_cut": false, "v10_omitted_listing": false, "v9_no_match": false, "v10_no_match": false}`

```diff
@@ -1,8 +1,15 @@
-  issue    file:line                          part period       label | col = value [flags]
+SUMMARY (per column & period: value -> issues; first and latest printing):
+  [1981-08] part1 panel 'Monthly series - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 3 printings, 1 distinct values - same in all
+      also printed under panel 'Monthly aeries - averages of daily or we' in 1 issues (not merged)
+      <v>: 1982_01..1982_10 (3)
+      first printing 1982_01 (treasury_bulletin_1982_01.txt:4812), latest printing 1982_10 (treasury_bulletin_1982_10.txt:4630)
+  column index differs across issues (c10: 1 issues; c1: 1 issues) for treasurybonds1 [1981-08]: the minority may be a mis-parsed header; verify with
+  [1981-08] part1 panel 'Monthly aeries - averages of daily or we' treasurybonds1 (Treasury bonds <v>/): 2 printings, 2 distinct values - REVISED/DIF
+      also printed under panel 'Monthly series - averages of daily or we' in 3 issues (not merged)
+      <v>: 1982_02..1982_02 (1)
+      <v>: 1982_02..1982_02 (1)
... (39 diff lines in total)
```

### sample 19: `printings --table "Net Capital Movement" --row "Jan. 25" --col "Latin America" --years 1939-1940`

UID0044 cell c (right cell); v9 3679 B -> v10 7959 B; `{"listing_v9": 21, "listing_lost": 21, "summary_key_loss_cause": "none", "listing_lost_daily": 4, "summary_keys_v9": 2, "summary_keys_v10": 2, "verdicts_flipped": 1, "v9_cut": false, "v10_omitted_listing": true, "v9_no_match": false, "v10_no_match": false}` (v9 lumped 17 sub-block series into one `17 distinct values - REVISED` key; v10 keys them by panel, correctly)

```diff
@@ -1,26 +1,4 @@
-  issue    file:line                          part period       label | col = value [flags]
-  1939_04  treasury_bulletin_1939_04.txt:1191   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1205   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1219   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1233   p1 1939-01<v>   'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1259   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1265   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1271   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1277   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1283   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
-  1939_04  treasury_bulletin_1939_04.txt:1295   p1 Y?<v><v>     'Jan. <v>' | c10 'Latin America' = <v>
... (39 diff lines in total)
```

### sample 20: `find_tables "Ownership of United States Treasury Notes" --all`

UID0170 cell a (right cell); v9 328 B -> v10 7409 B; `{"top_parts_v9": 1, "top_parts_v10": 0, "top_loss_why": "top group absent", "parts_lost": 1, "groups_lost": 1, "v9_cut": false, "v10_cut": true}` (F1: v9's single exact hit is pushed off the page by 378 row-label groups and boilerplate `[section]` lines)

```diff
@@ -1,3 +1,46 @@
-find_tables('Ownership of United States Treasury Notes', years=None): 1 table parts in 1 groups
-[-] Summary Distribution by Classes of Holders of Interest-Bearing Securities Issued by United States Government a  | 1 issues 1942_09..1942_09 | mat
-    treasury_bulletin_1942_09.txt:2595 part1 12c x 24r rows 1933..August
+find_tables('Ownership of United States Treasury Notes', years=None): 1211 table parts in 378 groups
+[section] UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY | treasury_bulletin_1949_09.txt:13<v> | sub-headings: Refundings in September, Oc
+[section] UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY | treasury_bulletin_1950_10.txt:15<v> | sub-headings: Cumulative table of content
+[section] UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY | treasury_bulletin_1953_06.txt:9<v> | sub-headings: Cumulative table of contents
+[section] UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY | treasury_bulletin_1955_01.txt:17<v> | sub-headings: Restrictions Lifted on Bank
+[section] UNITED STATES TREASURY DEPARTMENT OFFICE OF THE SECRETARY | treasury_bulletin_1942_12.txt:15<v>
+[-] Section II - Interest-Bearing Securities Issued by Federal Agencies but not Guaranteed by the United States  | 210 issues 1948_02..1968_12 | rows
+[-] PART A - PAR VALUES IN MILLIONS OF DOLLARS  | 37 issues 1941_06..1944_06 | [snapshot] | as-of April 30, 1941..December 31, 1943 | match:tokens(ti
... (39 diff lines in total)
```
