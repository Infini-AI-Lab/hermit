# v10 synthesis — lens: TOOLS

Coverage: **all 50 per-question analyses** in `q/` (verified count = 50; 175 cell trajectories: a 50, b 50, b2 25, c 50; 87 of the 175 cells wrong). This version replaces the earlier partial synthesis (26 questions). Counts come from a python pass over `fix.smallest_change`, `fix.alternatives`, `notes` and `cells.*.tool_use.friction`, followed by reading the free text of every TOOL-level fix and every friction field. Tool source was read: `../officeqa-pro133-v9-plumbing/packs/officeqa/tools/cli.py` (145 lines), `otables.py` (2276 lines), `harness_core/tools/calc.py` (118 lines), and the `workspace/README.md` usage text the model saw.

No answer values appear in this file (hard rule of ANALYST.md).

---

## 中文摘要（写给初学者）

### 先解释几个词

- **harness adapter（执行框架适配器）**：模型（这里是 GLM-5.3-flash）做题时所处的"外壳"，包含三层：给模型看的**文字**（AGENTS.md 里的建议）、**工具**（模型能在 shell 里调用的命令）、**控制**（提交时检查、循环保护）。本文只看**工具**这一层。
- **四个表格工具**：`find_tables`（按表号或标题词搜索表格）、`show_table`（把一张表解析后分页打印，附 NOTES 诊断）、`printings`（同一个格子在所有期刊里的历次刊印，用来找"最新修订值"）、`calc`（跑一段 Python，把代码和结果一起打印，供台账记录）。
- **cell（格）**：实验里"一个条件 × 一道题"的一次运行。四个条件：a（文字建议，温度 0）、b（无文字、提交时规则）、b2（文字 + 规则，只跑 25 题）、c（文字，温度 1）。
- **OCR（Optical Character Recognition，光学字符识别）**：语料是从 PDF 扫描转成的文本；表头、年份、数字常错位或错字。很多工具问题的根源就是 OCR 表头错位。
- **8000 字节页**：每个工具一次最多打印 8000 字节，超出就切页，模型要用 `--from-row N` 翻页。
- **FTS（Full-Text Search，全文索引）**：`find_tables` 用 SQLite 的 FTS5 索引按词匹配。现在的索引用 `unicode61` 分词器，**没有词干归并**（"dollar" 匹配不到 "dollars"），而且默认只搜标题和表头，不搜行标签。
- **identity（恒等式检查）**：`show_table` 会验证"总计列 = 各分项列之和"；某行不成立就标 `identity FAILS`，通常是 OCR 错字。
- **sibling printing（兄弟刊印）**：同一张表在相邻期刊里的另一次印刷；表头丢了年份时可以拿它来对齐。
- **first divergence（首次偏离）**：一个错格第一次走错的那一步（选错表、选错格、算错公式……）。

### 主要结论（按"多少题的修复需要它"排序）

1. **`find_tables` 的发现能力是最大的工具短板**：22 题的修复方案（首选或备选）改的是它。拆成五件事：(1) 默认搜行标签 + 前缀/词干匹配 + AND 无结果时退化为 OR（7 题；UID0182 三个错格可修）；(2) 先列所有"组"再列"部分"，一组不能吃掉整页（9 题；UID0243 c、UID0180 b 因此没看到正确表）；(3) 按查询词命中比例排序并打印命中的行标签/列名、标记快照表（4 题；UID0130 c）；(4) 索引正文章节标题并打印 `[section]` 命中（5 题；UID0215 四格全错，源头根本不是表）；(5) 索引完整性：标题跨过散文段落附着、OCR 标题纠正、无标题重印链接到有标题的孪生表（6 题；UID0133 b）。
2. **`show_table` 应该把它已经诊断出来的问题直接修好并打印出来**，而不是只警告：兄弟刊印年份自动对齐（4 题，UID0028 b、UID0027 b/c）、表头错位重对齐（6 题 + 4 题摩擦，UID0165 四格全错、UID0148 b）、恒等式失败行打印候选值（UID0007）、分组标题行和"合计 = 各行之和"脚注（4 题，UID0122 b2）、跨表"分项 ⊂ 汇总表"提示（UID0220 b2）。这些改动都只在检测条件成立时多打几行，分析都认为不会伤害已经答对的格。
3. **`printings` 的四个缺陷**：SUMMARY 用 `%g` 把 7 位数四舍五入成 6 位（3 题；UID0180 a 因此错，改两行代码即可）；按"期间 + 列"分组而不带面板/章节/截止日期，导致"REVISED"假警报和跨面板"最新刊印"（6 题；UID0007 的两次错误 gate offer 由此而来）；标题变了就跟丢系列、无标题重印跟不到（2 题，4 个错格：UID0062、UID0133）；查无结果时只说 "no matching row/column"（10 题；UID0114 四格为此找了 60 分钟不存在的值）。
4. **摩擦（不改变对错但烧步数）**：`--grep` 是纯子串、只过滤行、不认正则、不认 "1969-Nov"（37 题 59 格）；8000 字节切页（find_tables 21 题、show_table/--long 29 题、printings 24 题）；rg/sed 用相对路径找不到语料（28 题）；`officeqa.py` 搜索返回字典被当成列表打印（28 题）；zsh 引号/`===` 展开错误（21 题）；相同命令重复 3 次以上（35 题，其中 11 题失控到几十上百次——这是控制层的事，见 CONTROLS.md）。
5. **不要做的事**：不要为 UID0155 这类"参考答案取首次刊印"的题改工具（会伤害其它题）；外部序列工具（CPI-U、汇率）只在 3 题被提及，且分析都说它改不了这几题的评分，还破坏"封闭语料"前提——列为可选。

---

## 1. Method and headline numbers

- `fix.smallest_change.level` over 50 questions: **TOOL 17, NONE 18, TEXT 8, CONTROL 7.** 76 alternatives across the 50 files are TOOL-level, so most TEXT/CONTROL/NONE questions also name a tool change (usually as a friction fix).
- First-divergence stage of the 87 wrong cells: INTERPRETATION 21, EXTERNAL_KNOWLEDGE 19, SOURCE_TABLE 15, CELL_SELECTION 11, SOURCE_ISSUE_VINTAGE 7, EXTRACTION 5, FORMULA 4, OPERATIONAL 3, ROUNDING_FORMAT 2. Tool changes act mainly on SOURCE_TABLE, CELL_SELECTION, EXTRACTION and SOURCE_ISSUE_VINTAGE (38 wrong cells); they cannot touch EXTERNAL_KNOWLEDGE (19 cells, almost all CPI/FX questions) or most INTERPRETATION cells.
- Tool call totals (commands, as counted by the analysts): a: find_tables 586, show_table 490, printings 259, calc 172, other shell 1852; b: 482/261/81/63/741; b2 (25 q): 101/101/115/35/187; c: 289/228/144/105/451. Cell b ran printings only a third as often as a and never on 9 of its wrong questions; c used calc most consistently.
- "Count" below = number of **questions** whose `smallest_change` or an `alternative` calls for the change (task ids cited). Friction counts are separate (section 4).

## 2. Ranked tool changes

Ranking key: questions whose fix calls for it, then wrong cells it would flip, then cost. Each item states: the exact behaviour/output change; the cells' failures it addresses; whether any analysis says it could hurt a right cell; generality; implementation cost against the current source.

### T1. `find_tables`: search row labels by default, prefix/stem tokens, OR-fallback when AND returns nothing — 7 questions

Called for by: UID0182 (smallest; would fix b, b2, c), UID0029, UID0133, UID0180, UID0218, UID0237, UID0149 (alternatives). Friction: zero-hit first queries in 18 questions (section 4).

Behaviour change (otables.py `find_page`, lines 1779-1856, and `_fts_phrase`, line 1453):
- Build the FTS query over `{title header rowlabels}` always (today `rowlabels` is opt-in via `--rows`; keep the flag as a no-op).
- Emit every token of 4+ characters as a prefix term: `"canadian"* AND "dollar"* AND "liabilit"*`. FTS5 supports the `"tok"*` syntax without reindexing. Alternative: rebuild the index with `tokenize='porter unicode61'` (schema line 1350; bump `INDEX_VERSION`). UID0182 verified against the pilot's `tables.sqlite` that the question's verbatim query returns only FCP tables today and returns CM-I-1 and CM-III-1 with rowlabels + prefix.
- When the AND query returns 0 parts, retry with the tokens OR-ed and print `no table has all words; best partial matches (N of M words):` (UID0029 a[5]/c[4]/c[12], UID0180 a[5]/b[6]/b2[5]/c[4], UID0218, UID0224 c[4], UID0225 c[6]/c[8], UID0245 b[4], UID0097 a[4]/a[6] all lost their first call to a 0-result phrase).
- Print the matched row labels on the group line: `match:rowlabels (Total liabilities to all foreigners; Canadian dollars)`.
- Singular/plural tolerance falls out of prefix matching (UID0237 b[8]: 'Classes ... Types' vs 'Class ... Type').

Failures addressed: UID0182 b/b2/c never found CM-I-1 (SOURCE_TABLE) and built the answer from FCP tables; cell a found it only on its fifth phrasing. Could hurt a right cell: "unlikely" (UID0182: ranking stays title > header > tokens; residual risk that a second candidate, CM-III-1, becomes visible and is picked). Generality: document_qa_general. Cost: **low** — ~15 lines in `find_page`/`_fts_phrase`; the OR retry is one extra `con.execute`. Porter tokenizer needs an index rebuild (prepare time, `cli.py build`).

### T2. `find_tables`: group-first listing, a cut removes parts never groups, per-group cap under `--all`, row-period span and column preview on the group line — 9 questions

Called for by: UID0243 (smallest; would fix c), UID0180 (alt; b's `--all` page was one 314-issue group), UID0133, UID0135, UID0094, UID0007 (row-year span), UID0154, UID0237, UID0149 (alts). Friction: find_tables page floods/cuts in 21 questions, 32 cells.

Behaviour change (`find_page` lines 1832-1855):
1. Print the group line for every ranked group up to `--limit` first; each with `rows <min first_period>..<max last_period>` over its parts (columns `first_period`/`last_period` already exist in table `t`) and the first four value-column labels from the stored `header` column.
2. Only then list parts (latest 3, or all with `--all`), stopping when the byte budget is reached; under `--all` cap one group at ~12 parts and print `... N more (use --all --years)`.
3. Merge groups whose normalised title differs only by a leading `Table N.` (UID0243 b[17]: 1960s 'Table 1. Summary of Federal Securities' splits into one-issue groups because the key is `norm(ttl)[:60]`, line 1826).
4. When more than ~10 groups would print, list the rest as one line each (UID0135, UID0094).

Example line the model would have seen (UID0243): `[-] Summary of Federal Securities | 108 issues 1960_01..1968_12 | rows FY1954..1968-Dec | cols: Total outstanding > Total 1/ | Total outstanding > Public debt 2/ | ...`.

Failures addressed: UID0243 c (right table cut off the page twice, c[8], c[14]); UID0180 b (OFS-1 never visible next to FD-1); UID0133 a[7] lost the modern CM-I-1 group; UID0220 b2[8]/c[8] (component tables filled the page, the Summary table was cut). Could hurt: "unlikely" (UID0243: superset of what b saw; per-part lists remain under `--all`). Generality: document_qa_general. Cost: **low-medium** — ~40 lines restructuring the listing loop; no index change.

### T3. `find_tables`: rank by fraction of content tokens matched, print where they matched, mark snapshot tables — 4 questions

Called for by: UID0130 (smallest; would fix c), UID0182 (matched labels), UID0220 (component-of line on the group), UID0183 ('no title match' line).

Behaviour change (`find_page` lines 1810-1818): today `where` is set only when the whole normalised phrase is a substring of title or header, so any question phrase with extra words scores 0 and the list falls back to group-size order (UID0130 c[6]: TSO-1 at rank 14 behind a 2025-era group). Score each group by the fraction of content tokens (drop stopwords and 1-letter tokens like 'u', 's') found in the title and in any single row label or column header; print `match:title(federal,securities) row:'Total Federal securities'`; mark tables whose parts carry an as-of date and no period column as `snapshot as-of month-end`. When no title/header token matches at all print `no title match for <words>` first (UID0183 a[10]-[20], b[210]-[214], c[11] got hundreds of body-text rows).

Failures addressed: UID0130 c (took the first plausible group at temperature 1). Could hurt: "unlikely" (a and b2 picked TSO-1 from rank 7 already). Generality: document_qa_general. Cost: **low** — ~20 lines.

### T4. Index narrative section headings; print `[section]` hits; a `show_section`/`find_text` reader — 5 questions

Called for by: UID0215 (smallest; would fix a, b, b2, c — all four wrong), UID0133 (section heading match), UID0183, UID0204 (`find_text` for prose), UID0037 (say that a figure caption exists at the searched words).

Behaviour change: at build time scan every issue for upper-case headings (`_is_caps_heading`, otables.py line 325, already exists) and their sub-headings, store `(file, start, end, heading)` in a new table; `find_page` prints a `[section]` block above table groups when query tokens match a heading, e.g. `[section] TREASURY FINANCING OPERATIONS, OCTOBER-DECEMBER 1987 | treasury_bulletin_1988_03.txt:1688-1800 | sub-headings: Auction of 2-Year Notes (:1692) ...`, and when a query matches 0 table parts: `no table matched; N narrative sections match these words`. Optionally `show_section <file> <line>` prints the section with line numbers (a discoverable `sed -n`).

Failures addressed: UID0215's four cells all searched the literal source name with find_tables and were sent to PDO-3 (a table) instead of the narrative; b and b2 even grepped the narrative and lost it to `head -100`/`-m 10` truncation. Could hurt: "unlikely" (block is empty on table questions; slight page cost when headings and titles share words). Generality: document_qa_general. Cost: **medium** — ~60 lines in `build_index` + schema + `INDEX_VERSION` bump, ~20 lines in `find_page`; the heading regex exists.

### T5. Index completeness: caption attachment across prose, OCR-title normalisation, id normalisation, untitled-reprint linking, orphan continuation parts — 6 questions (+2 friction)

Called for by: UID0225 (smallest; friction only, no wrong cell), UID0133 (smallest; would fix b), UID0062 (alt: 'Expeditures' -> 'Expenditures', show the panel subtitle), UID0019 (alt: 'P-IV-1' -> 'FCP-IV-1' inside a SECTION IV banner), UID0109 (alt: attach the orphan middle part of a 3-part table), UID0117 (alt: 1970s PDO-1 parts unindexed by title). Friction: UID0230 (the 1960_10 narrative table is '(untitled)'), UID0102 (capsule tables are unindexed continuation parts).

Behaviour changes (`file_tables`, otables.py lines 333-498):
- The context walk stops at the first long narrative line (line ~412 "long narrative line: stop looking further up"). Continue past at most two prose paragraphs, within the same section, when an `ID_RX` caption line sits within ~12 lines above the header (UID0225: 'Table MQ-1' caption at 1982_03:4367, table at :4373 — the one part every cell needed was never listed).
- Normalise OCR-damaged title words with a small edit-distance match against the set of known titles of the same section (UID0062 c[7] skipped the '[-] Expeditures' group).
- Map `P-<roman>-<n>` under a `SECTION <roman>` banner to `FCP-<roman>-<n>` (UID0019: the June sterling table had id '-', so printings reported one printing instead of two).
- Link an untitled part to its titled twin by fingerprint (section, units line, row-label set, column count); give it the twin's id with a flag `[untitled; linked to CHART CM-A by shape]` (UID0133 b[26]: the 2013_12 reprint carried the final value and was invisible to printings and to the later_printing rule).
- Attach an orphan part between two parts of the same table when row labels match and column ordinals continue (UID0109 b[18]/b[20]: 2020_03 lines 715-737 parsed as 'TABLE of the').

Failures addressed: UID0133 b (SOURCE_ISSUE_VINTAGE via an unfollowable reprint). Could hurt: UID0133 says "unlikely" but warns a wrong shape match would add a mislabelled line, so require identical row-label set and column count. Generality: document_qa_general. Cost: **medium-high** — the caption change is ~10 lines but sits in the cached parser, so every title in the corpus must be diffed after the change; the fingerprint link is ~60-80 lines at build time.

### T6. `show_table`/`printings`: anchor missing years from a sibling printing automatically, in every view including `--raw` — 4 questions

Called for by: UID0028 (smallest; would fix b on hop 1), UID0029 (smallest; no wrong cell, but it is the fix for the v8 cards failure and the detour in all three cells), UID0027 (alt; would fix b and c — the structural failure that sank all three v8 arms), UID0117 (alt: derive block years from description dates).

Behaviour change: the tool already prints 'N column groups share the period stub ... unstacked as panels', flags rows `YEAR_UNKNOWN` with periods `Y?k-MM`, and lists `SIBLINGS` (`siblings()`, line 1734). Go one step further: take the nearest sibling of the same id and width that has year labels, align by exact cell agreement of one full row block per panel, adopt its `year0` per group, fill periods as for a labelled table, and print in NOTES and in the period column: `YEARS ANCHORED from treasury_bulletin_1970_05.txt:4843 (240/240 cells agree): panel1 = 1959,1960,1961; ...; panel4 = 1968,1969,1970 (partial, ends 1970-05)`; rows read `1964-02 <anchored>` instead of `Y?2-02`. If no sibling aligns, print `no sibling aligns - years unknown`. The `--raw` branch (`show_page` lines 1887-1906) prints no NOTES at all today; b in UID0028 and UID0027 used `--raw` for every view and never saw the tool's own analysis — print `note_lines` above `RAW LINES:` in that branch too. `printings --table AY-1 --row 1964-02` would then resolve in the unlabelled issue.

Failures addressed: UID0028 b (built its own year map from a raw sibling header and got it wrong), UID0027 b and c (b[15] `--raw`, c[15] `--from-row 1`: both hid the SIBLINGS note). Could hurt: "unlikely" in all three analyses — anchoring prints only when a sibling matches cell-for-cell and the raw labels stay visible. Generality: document_qa_general. Cost: **medium** — ~60-80 lines in `analyze`/`_panels_for`/`_fill` (a `year0` override path; the code path that fills periods from a labelled header exists), plus ~6 lines in the raw branch.

### T7. `show_table`: repair header shifts instead of only warning; detect the mirror cases — 6 questions (+4 friction)

Called for by: UID0165 (smallest; all four cells wrong), UID0148 (smallest; would fix b), UID0213, UID0214, UID0077, UID0228 (alts). Friction: UID0102 (shifted labels made a discard the right table family for 35 steps), UID0243 (column labels changed between calls, c[23]/c[55]/c[80]), UID0101 (noisy 'header probably shifted' on every column), UID0225 (MQ-1 header unusable).

Behaviour changes (otables.py: the 'LAST labelled column(s) ... spurious extra label' branch ~line 1150; the label/type conflict block ~line 1220; `_identities` line 687):
1. **Spurious trailing label**: when the header has k+1 labels for k data-bearing columns and the labels carry ordinals (1)..(k) with exactly one unnumbered parent-only label, drop it and shift the labels right of it one column left; confirm with the identity (`c3 = sum c4..c12`) and a sibling printing; print `v9 = c9 Mutual funds 3,7 (9) [num] [LABELSHIFT-1]` and a NOTES line naming the sibling that agrees (UID0165: OFS-2 in 2007_03..2010_03; cell a read the values off the same page that printed the warning and still took the label column).
2. **Amount-like label holding dates** (UID0148 1972_05 MQ-1: 'Amount outstanding > 13-week.1' holds dates): mirror the existing date/num check; when the label carries a pandas '.1' dedup suffix and its base sits on a num column, borrow the label from a sibling printing whose same-type column carries a date word and print `c2 Issue date > 13-week [date] (label repaired from 1974_05:4599)`.
3. **Identity-based Total relabel** (UID0214, UID0077, UID0228, UID0213): when the identity says c9 = sum c1..c8 but the 'Total' label sits on c8, print `HEADER SHIFT: label 'Total currencies' is on c8 but c9 = sum c1..c8 on 24/24 rows; --match/--col 'Total' resolves to c9` and make `--match` follow it. Header has fewer labels than data cells: print `header has N labels for M data columns: labels right of column k may be shifted by one`.
4. Apply the same realignment inside `printings` (see T10).

Failures addressed: UID0165 a/b/b2/c (CELL_SELECTION on a mislabelled column), UID0148 b (b[18] 'tool mislabels columns'), UID0214 b[31] wrong column via `--match`. Could hurt: "unlikely" (UID0165: fires only when the trailing column is empty in every row, ordinal count equals data columns and the identity holds after the shift; else the old note stays). Generality: document_qa_general. Cost: **medium** — ~25 + 8 + 20 + 15 lines, all local to `analyze`.

### T8. `show_table`: identity-FAILS repair line with the cross-printing majority value — 1 question (3 wrong cells)

Called for by: UID0007 (smallest; would fix c and the first submits of b and b2). Related: UID0218 notes (run the row identity inside printings so a column-shifted printing is labelled 'IDENTITY FAILS - misaligned' rather than 'REVISED').

Behaviour change: for every row where the total identity FAILS (`_check_identity` already returns the bad lines), scan the other parts of the same table id (the bounded newest-first machinery of `latest_printing`, line 2233) keyed by part, panel stub label, row period and column, and print only when the residual and the majority agree: `identity FAILS line 374 (CY1943, panel 'Budget expenditures'): cells sum short of the printed total. Suspect c7 Jul.: this issue <v_this>; N of M other printings print <v_major>, which restores the total. Likely OCR misread; use <v_major>`. Rows that differ between issues but still sum (genuine revisions) get no line.

Failures addressed: c and b2 had the FAILS note on screen (c[10], c[12]; b2[9], b2[11]) and the AGENTS.md sentence about it, and skipped it; only a did the comparison by hand over ~25 steps. Could hurt: "unlikely" (raw cell stays visible next to the candidate). Generality: document_qa_general. Cost: **medium** — ~50 lines; adds a bounded scan (~6 ms per part) to `show_table` calls on tables with FAILS rows only.

### T9. `show_table`: keep group-header rows, print row footings and the identity beside the cell, footnotes inline — 4 questions

Called for by: UID0122 (smallest; would fix b2, likely b and c), UID0243 (alt: identity line under the used cell), UID0096 (alt: footnote text inline after a footnoted row label + composition of each Total), UID0044 (alt: note when a panel has fewer rows than its siblings).

Behaviour change: the segment builder (~lines 1164-1180) `continue`s on every 'section' row, so the CELLS (`--long`) view never shows a group header while the ROWS view draws `== header` and runs it to the next header (UID0122 b[15]: the receivable and the total appear inside the currency group). Emit the header as its own line in `--long` (`5232 p1 - 'Foreign exchange and securities:' [group header, no values]`); in ROWS say `(group header at line 5232; no subtotal row; membership of the rows below is not marked)`; flag total rows `total`; add a row identity in NOTES: `row identity: 'Total assets' (5236) = <5 rows> (holds in all 3 columns)`. UID0243: when the used column is the total of a verified identity, print one line under the cell, not only in NOTES. UID0096: `Free list 3/  [3/ Duty-free commodities taxed under ...]`.

Failures addressed: UID0122 b2 (INTERPRETATION driven by a view that hid the header), b and c (took the tool-drawn segment as membership). Could hurt: "unlikely" (one extra line per header row). Generality: document_qa_general. Cost: **low-medium** — ~20 lines in the segment builder, ~30 for row identities.

### T10. `printings`: key cells by panel/section and as-of date, warn when the column index differs across issues — 6 + 6 questions

Called for by (panel/section/as-of keys): UID0007 (alt + notes: "root tool defect"), UID0044, UID0170, UID0211, UID0130, UID0220. Called for by (column-index/shift awareness): UID0188, UID0228, UID0165, UID0214, UID0220, UID0218. Friction: false REVISED/DIFFERS or cross-panel 'latest printing' in 14 questions, 31 cells.

Behaviour changes (`printings_data`/`printings_page`, lines 2109-2227; `latest_printing`, line 2233):
- Carry `o["section"]` (the analysed rows already have it; `show_page` line 1985 uses it) and the part's `asof` into each found record; print the section on every listing line; group the SUMMARY by `(part, section, column, period, asof)`; accept `--section <substring>`. Today `bycol` (line 2201) keys on (part, column, period) only, so three stacked panels sharing year rows produce a 'latest printing' from another panel (UID0007 a[13]/a[24]/a[32], b2[17], c[17] and both bogus gate offers), two same-title tables merge into one 'REVISED' block (UID0211 a[14]/a[35]/a[61], b[17], b2[16], c[24]), four same-issue panels print as '4 printings, 4 distinct values' (UID0044 a[20], b[16], c[20]), and three semiannual surveys read as revisions (UID0170 a[32], c[25], b2[33]).
- `latest_printing` (used by the later_printing rule) must match the section/panel stub as well as period and column key (line 2264-2267).
- When the same column label is matched at different column indexes across issues, print before the SUMMARY: `column index differs across issues (c7: 7 issues; c8: 3 issues): header may be mis-parsed; verify with show_table --long` and do not call the minority `ISOLATED ... likely OCR/row shift` (UID0188 a[13]/b2[20] called the correct column the outlier; UID0228 a[18]; UID0165 b2[43]/c[99]; UID0214 b2[23]).
- Run the row identity (from `_identities`) per printing and label a failing printing `IDENTITY FAILS - misaligned` instead of counting it as a distinct value (UID0218 b[10]).
- Snapshot tables (no period column, as-of in the title): print `snapshot table: one printing per as-of date` and key by as-of (UID0130 a[10]/[20], b[36], b2[10]/[19]).

Failures addressed: none flips alone by the analysts' reckoning, but UID0007's b/b2 gate offers and UID0211 a/b's revision hunt came from these outputs, and the later_printing control inherits the defect. Could hurt: no analysis says so; UID0170 warns the opposite (with 'latest printing wins' text, a later survey would silently replace the right one). Generality: document_qa_general. Cost: **low** — ~15 lines for keys, ~10 for the index warning, ~15 for identity-in-printings.

### T11. `printings`: print exact cell text in the SUMMARY (drop `%g`) — 3 questions

Called for by: UID0180 (smallest; would fix a), UID0135 (alt), UID0122 (c[53]: the rounded summary entered calc at c[62]).

Behaviour change: lines 2208 and 2213 build the value keys with `"%g" % val`, which rounds to 6 significant digits and switches to e-notation above 1e6; group 'distinct values' on the exact printed text (commas, r/p flags and footnote marks stripped). The model would see `<full integer as printed>: 2006_12..2011_09 (20)` instead of a 6-digit mantissa. Latent defect: two printings differing beyond 6 digits are reported 'same in all printings'.

Failures addressed: UID0180 a (tailed the output and summed four rounded values; the a/c split on that question is purely this formatting). Could hurt: "no". Generality: domain_general. Cost: **trivial** — 5 lines.

### T12. `printings`: informative no-match and end-of-series messages; label normalisation — 10 questions

Called for by: UID0114 (smallest; all four cells wrong, no correctness claim), UID0218 (smallest; friction), UID0057, UID0097, UID0122, UID0148, UID0109, UID0180, UID0225, UID0170 (alts/notes). Friction: 'no matching row/column found' in 24 questions, 35 cells.

Behaviour change (line 2185 prints the generic message): replace it by a diagnosis chosen from what the index already knows:
- Period outside the table's printed span: `MY-2 prints periods 1991-01 .. 2002-12; the last issue printing this table is 2003_03; later issues do not print MY-2. Note in treasury_bulletin_2003_06.txt:2169: "... discontinued ..."` (grep the next issue's section for 'discontinued' within ~50 lines of the id). UID0114: every cell had read the discontinuation note and still hunted the value for 20-60 minutes because the tool's message read like a lookup mistake.
- `--col` matched nothing: list the table's column labels (`columns in <file:line> are: c1 ... | c4 Redemptions 1/ > Total | ...`), after stripping footnote marks (`\s*\d{1,2}/` anywhere) from both sides (UID0218 a[38]/a[54]/c[33]; UID0057 a[18] six silent misses after a column rename).
- Row window: `rows FY2011-FY2014 were last printed in 2016_09; your --years 2020-2025 starts after that` (UID0109 b[14] blamed the label).
- Periods as columns: `this table has periods as columns; use --col "June 30, 2000" --row <label>` (UID0122 a[10]/c[17], UID0097 a[15]/a[17], UID0096 c[29]-[33]).
- Rows keyed by a date cell: `rows of MQ-1 are keyed by c2 Issue date; pass that date` (UID0148, UID0225 — ten calls across cells).
- Normalise OCR-split labels by deleting intra-word spaces before matching (`To tal as sets`, UID0122 a[23], c[44]-[48]); accept `--table "Table 2 in <SECTION>"` by number + section.

Failures addressed: none for correctness; ends the three longest hunts of the pilot (UID0114 a 3600 s, b/b2 ~1400 s each). Could hurt: "no". Generality: document_qa_general. Cost: **low** — ~40 lines in `printings_data`/`printings_page`; a `SELECT min(first_period), max(last_period)` per tid.

### T13. `printings`: SUMMARY first, compact per-issue listing, multi-cell/range mode — 9 questions

Called for by: UID0224, UID0062 (keep SUMMARY ahead of the listing), UID0077 (`--rows 2010-01..2011-12 --latest` printing one line per period plus a SUM line), UID0155 (several `--col` and `--summary`), UID0135 (several periods per call), UID0114, UID0122, UID0243, UID0101 (80 cells clipped at 60). Friction: printings pages cut in 24 questions, 40 cells; 12-48-call printings loops in one command truncated in the middle in 9 questions.

Behaviour change: build the SUMMARY lines first and reserve their bytes (via `_fit_lines`) so daily/weekly rows cannot cut it (UID0224 a[12], b[8], b2[8], c[12]; UID0062 a[15] lost the REVISED line; UID0243 c[34]-[42]); a month key should match only rows whose filled period is that month unless `--all-periods` (UID0224: 72 cells for one month); one line per issue `issue value flags` (~20 bytes instead of ~100); `--rows A..B` / repeated `--col` / `--summary` so a 24-month or 4x4 sweep is one call (UID0077 a[50]-[58], b[41]-[51], c[49]-[55]; UID0155 a[8], b2[12], c[13]; UID0114 b2's 78 KB loop re-run ~45 times).

Could hurt a right cell: **yes, once** — UID0155 states that b2's truncated sweep is what accidentally produced its right answer; with complete output it would have followed the REVISED line to the same graded-wrong value as a/b/c (the key follows the first printing on that question). This is a property of a disputed question, not a reason to keep truncation. Generality: document_qa_general. Cost: **low-medium** — ~10 lines to reorder, ~40 for the range mode (parser in `cli.py` + loop in `printings_data`).

### T14. `printings`: follow a series across a title change; print the revision footnote next to an `r` value — 2 questions, 4 wrong cells

Called for by: UID0062 (smallest; would fix b, c, likely a), UID0133 (smallest; T5's link is its precondition).

Behaviour change: `_printing_parts` (line 2093) matches `--table` by id or by an FTS phrase on the title, so a renamed table splits one series into two and the old-title half reports 'same in all printings' with one printing (UID0062 b[16], c[16]: false completeness). After the SUMMARY, when the title match covers fewer than 3 issues or the same column label + row period exists in a later issue of the same section under another title, print `SERIES CONTINUES under another title: "<title>" (69 issues 1948_03..1954_03) prints FY1947 | "Department of the Army" too; latest printing 1953_02 (<file:line>) = <value> r - run printings --table "<title>" ...`. For any cell flagged `r`, append the nearest revision footnote below the table as one line (UID0062 1948_12:326, a reclassification note that cell a lacked when it chose the older vintage).

Could hurt: "unlikely" (adds lines; a question that pins an early issue still sees both values). Generality: document_qa_general. Cost: **medium** — ~40 lines for the continuation scan (bounded to the same section, later issues), ~15 for the footnote grep.

### T15. `show_table --grep` semantics: substring hint, OR, period forms, column hint, ranges — 11 questions (largest friction item)

Called for by: UID0170, UID0224, UID0237, UID0100, UID0112, UID0102, UID0057, UID0149, UID0214, UID0220, UID0182. Friction: `--grep` returning 0 rows in 37 questions, 59 cells; regex alternation in 10 questions.

Behaviour change (line 1984-1985: `g in norm(label) or g in norm(period) or g in norm(section)`):
- 0 rows: print `0 rows: --grep is a case-insensitive substring of the filled period (1942-12), the raw label or the section; not a regex. "Judicial" is column c2 - use --match` (UID0100 a[6]/a[8]/b[11]/c[8]; UID0170 c[10]; UID0109 b2[8]/c[8]; UID0101 b[14]/c[27]; UID0182 a[14]).
- Accept `|` or repeated `--grep` as OR (UID0237 c[10]/c[26]/c[28]; UID0112 a[35]; UID0062 a[11]; UID0122 a[29]; UID0182 b[26]/[29], b2[14]; UID0215 b2[64]).
- Normalise month-name period forms (`1969-Nov`, `Nov 1969`, `1942-Dec`, `January`) to `YYYY-MM` via `parse_query_period` (line 206) and match the filled period too (UID0214 a[20]/b[39]/b2[13]; UID0220 a[10]/b2[11]; UID0224 a[8]; UID0057 b[27]; UID0213 c[34]).
- Word-anchor or say so: `--grep TAB` matched 'marke**tab**le' (UID0149 c[15]).
- `--period FY2021` / `--grep 1996,1997,...` keep exactly those rows and print `FY2021 = 2020-10 .. 2021-09: 12 month rows (excluded: 2020-09 belongs to FY2020)` (UID0102 a[50]/c[58]/b[25]; UID0112 a[35]).
- Note: `--from-row` combined with `--grep` yielded nothing in UID0007 a[50]-[51] and UID0245 a[40]..a[250] ('page rows 0-0 of 12', 22 identical cycles): `from_row` indexes the filtered body (line 1992), which is correct, but the header says `of 12` while the page is empty — print `--from-row 12 is past the last filtered row (12)`.

Could hurt: no. Generality: domain_general. Cost: **low** — ~25 lines.

### T16. `show_table`: say when the line is not a table start; multi-part navigation; `--long` paging — 7 questions

Called for by: UID0094, UID0037, UID0214 (non-table line), UID0154, UID0029, UID0133, UID0148, UID0112 (paging/parts). Friction: silent snap to a neighbouring table in 15 questions, 19 cells; `--long` page overflow in 17 questions.

Behaviour change: `table_at` (line 501) falls through to "the next table"; print `no table starts at <file>:830; showing the next table at :835 (Total Cash Income and Outgo); nearby: :875 ...` or, for a line inside a body, `line 1295 is row 15 of TABLE MS-1 starting at 1281` (UID0094 b[9]/c[8], UID0037 a[29]-[30] jumped to an unrelated table, UID0214 a[20] empty page, UID0077 a[19]-[23], UID0211 a[56]/a[69], UID0225 a[8]). For a chart/figure caption say `figure values are not in the text corpus` (UID0037). Multi-part tables: `--from-row` beyond part 1 returns an empty page (UID0133 c[18], c[26]: the grand total in part 2 was unreachable) — print `this part has N rows; the table continues in part 2 at :LINE (use --stitch)`. `--long` spends 11 cell lines per row and overflows after 5-7 rows (UID0154 b2[11], UID0094 c[13]-[16], UID0044 a[9], UID0148 a[13], UID0149 a[13]) — after a cut `--long` page print `these N rows fit one page without --long, or with --cols`. Usage text: `--from-row` is 0-based and the continuation line gives the value to pass (UID0148 a[15]-[16], UID0149 a[20] skipped row 0 with `--from-row 1`).

Could hurt: no (UID0094: refusing instead of snapping must keep the `title_line` resolution that many cells rely on, e.g. UID0097 a[21], UID0155 b2[16]). Generality: domain_general. Cost: **low** — ~20 lines.

### T17. `show_table`: flag OCR-shifted or duplicated rows; component-of cross-table note; row export — 3 + 1 + 2 questions

- Flag parse-shifted rows (UID0019 alt: blank date labels + a run of duplicated rows; UID0215: 1991_09/1991_12 PDO-3 rows shifted and unmarked so the 'latest printing' rule selected corrupted cells, c[45], b2[59]; UID0230: garbled narrative table without any flag). Output: `5 rows have no date label (lines ...); rows 2078-2082 repeat rows 2052-2056 - labels after the first blank are probably shifted; prefer another printing`. Cost low (~20 lines in `analyze`).
- Component-of cross-reference (UID0220 smallest; would fix b2): a same-issue, same-section pass matching each table's first value column against the value columns of the other tables (equal on >=80% of >=5 shared periods); print under NOTES `c1 'Total' of this table = c9 'Expenditures > General' of TABLE 'Summary of ...' (<file:line>), which also has c8 'Expenditures > Total'`. Could hurt: "unlikely" (additive, fires only on a numeric identity). Cost medium (~50 lines; needs the other tables' analyses of the same issue, cached by `analysis_at`).
- Row export (UID0007 alt, UID0148 alt): `show_table --values <line>` or a calc helper `rows('file', line)` returning a row's cells as a Python list with provenance, so 80-value lists are not hand-typed (UID0007 c[20] typed 78 of 80; UID0148 c[23] slipped one row; 7 questions hand-typed lists). Cost low (~15 lines) but it crosses the tool/calc boundary (calc runs in a subprocess without the corpus path).

### T18. `calc`: half-up rounding helper, scale note, larger result clip — 2 + 1 + 2 questions

- `round_half_up(x, n)` in the RUNNER scope and a note when a printed result sits within 1e-9 of a half boundary (UID0225 a[52]/a[62], b2[25]; UID0102 a[59]/c[65]). Cost trivial (~8 lines in `calc.py` RUNNER + README line).
- One-line scale note when the code contains a literal `*100`/`200*` factor: `note: expression multiplies by 100 (percent form); the bare fraction is <value/100>` (UID0245 alt; the smallest change there is the submit-time scale check). Cost trivial (~8 lines in `main()`); UID0245 rates it weaker than the gate.
- `MAX_RESULT` = 2048 bytes truncated calc output before the needed minimum (UID0028 a[24]/[26]/c[55]; UID0029 a[33]): raise to 8 KB or print `... [truncated: N more bytes; print less]`. Cost: one constant.
- `statistics.NormalDist` note in the README (scipy absent: UID0165 a[558]/b[43]/b2[39]/c[55], UID0214 c[41]/c[55]); a formula library (`midpoint_pct_change`, `cagr`) is UID0101's alternative, rated weaker than one sentence of text.

### T19. New tools / environment (optional, lower rank)

- **Reference-series lookup** (`series cpi-u --month 1969-11 --yoy`, `refseries cpi-u --annual`, `lookup_series USD/INR`): UID0214, UID0243, UID0183 (alts). Would collapse the four-way CPI recall scatter and the 10-70-step CPI/FX hunts seen in UID0122, UID0213, UID0214, UID0228, UID0243, UID0183; but every analysis says it would not change the grade on its question (UID0214: the key's reading differs anyway; UID0243: the column choice is the decisive step; UID0183: the key's convention is not the period average) and it changes the closed-corpus premise. officeqa_specific. Cost: data file + ~30 lines. Cheaper alternative from UID0165 notes: a corpus-level line in the usage text stating which external series the corpus does not contain.
- **Corpus line reader** (`lines <file> a-b`, or `show_section`): resolves bare filenames the way the table tools do; rg/sed with a relative path failed in 28 questions, 39 cells (UID0112, UID0215 alts). Cost: ~20 lines in `cli.py` using `load_lines`. The README could instead state once that rg/sed need the absolute corpus path.
- **`officeqa.py` search example**: `docs.search(...)` returns a dict; the README example should read `hits = docs.search(...)["results"]; for h in hits: print(h["filename"], h["start_line"], h["snippet"])` (UID0225, UID0133, UID0114; friction in 28 questions, 41 cells; the v8 analysis recorded the same failure). Text-only change, zero code.

### Usage-text (README) changes implied above

One query per command (multi-query commands lose output to the 8000-byte cap: 9 questions); `--grep` is a substring over label/period/section, not a regex, not a column filter (`--match` is); `--from-row` is 0-based, pass the number the continuation line prints; `printings --row` takes the row *period or label*, `--col` the column — for tables with periods as columns swap them; quote multi-word values (UID0165 c[12]/c[33], UID0245 c[19]); `printings` has no `--all`, `show_table` has no `--table` and takes `<file> <line>`, not `file:line` (usage errors in 17 questions); `statistics.NormalDist` replaces scipy; rg/sed need the absolute corpus path; `find -printf` and `cat -A` are not available on this macOS (4 questions).

## 3. Things the tools already do right (keep)

- `find_tables` per-part `as-of <DATE>` tag: decisive in UID0154 (all four cells; both v8 wrong arms lacked it) and UID0170.
- `printings` per-cell SUMMARY `N printings, k distinct values - REVISED/DIFFERS ... latest printing <issue>`: decisive in UID0019, UID0100, UID0224, UID0135, UID0077, UID0112 (kept every cell right). The changes above (T10-T14) refine its keys and formatting; they must not remove this block.
- `show_table` identity note (`c1 = sum c2+c3 on 21/21 rows`): what let a and c trust the summary table in UID0112 and what T7-T9 build on.
- `calc` printing several conventions in one run (UID0100 b[22], c[17]) and the ledger frame.
- `--raw` as the escape hatch when the parse is garbled (UID0225, UID0148) — keep it, but give it the NOTES (T6).

## 4. Tool friction patterns (counts over the 175 cell friction fields; keyword match, hand-checked on the top items)

| Pattern | Questions | Cells | Notes / representative steps |
|---|---|---|---|
| `show_table --grep` returned 0 rows (substring only, row-only, period spelling) | 37 | 59 | UID0100 a[6]; UID0214 a[20]; UID0224 a[8]; UID0237 c[10]; see T15 |
| Identical command repeated >=3 times | 35 | 59 | runaway (>20 repeats, up to ~300): UID0030 a, UID0037 a, UID0114 a/b/b2, UID0165 a, UID0183 a/b, UID0188 a/b, UID0245 a, UID0211 a, UID0100 a, UID0029 b (11 questions); a control matter, see CONTROLS.md |
| `show_table` page cut / `--long` overflow / paging confusion | 29 | 39 | UID0154 b2[11], UID0094 c[13]-[16], UID0148 a[13]; see T16 |
| rg/sed on a bare or relative corpus filename (corpus not in cwd) | 28 | 39 | UID0112 a[11], UID0182 a[18], UID0225 a[18]; see T19 |
| `officeqa.py` search helper misuse (dict repr, exit 126) | 28 | 41 | UID0225 a[44]-[50], UID0188 a[19]-[23]; see T19 |
| `printings` output cut or overlong (SUMMARY lost, loops truncated) | 24 | 40 | UID0224 a[12], UID0062 a[15], UID0155 b2[12]; see T13 |
| `printings` 'no matching row/column found' with no diagnosis | 24 | 35 | UID0114 a[37], UID0218 a[38], UID0148 c[17]-[31]; see T12 |
| Header/column shift or mislabel (OCR), including `--match` binding the wrong column | 22 | 40 | UID0165 a[12], UID0148 b[17]/[19], UID0214 b[31], UID0228 a[18]; see T7, T10 |
| `find_tables` page flood/cut (one group eats the page; `--all` without `--years`) | 21 | 32 | UID0243 a[6], UID0180 b[10], UID0130 c[6]; see T2 |
| zsh quoting / glob / `===` expansion errors | 21 | 30 | UID0114 b[31]/b2[220]/c[55], UID0112 a[28]; usage-text item |
| `find_tables` zero hits for the question's phrasing | 18 | 20 | UID0182 a[4]/b[6]/c[5], UID0180 a[5]; see T1 |
| Usage error exit 2 (nonexistent option, `file:line`, `--table` on show_table) | 17 | 22 | UID0122 a[8], UID0077 c[10], UID0109 b[8] |
| `show_table` silently snapped to a neighbouring table / non-table line | 15 | 19 | UID0094 b[9], UID0037 a[29]-[30], UID0077 a[19]-[23]; see T16 |
| `printings` false REVISED / cross-panel or cross-survey merge | 14 | 31 | UID0007 a[13], UID0211 a[14], UID0044 a[20], UID0170 a[32]; see T10 |
| `--stitch` misaligned or mislabelled continuation columns | 12 | 12 | UID0007 b[12], UID0096 b[46], UID0117 b[16]; partly T5/T7 |
| Regex alternation passed to `--grep`/rg | 10 | 11 | see T15 |
| Several tool calls chained in one command, output truncated mid-way | 9 | 14 | UID0155 b2[12], UID0112 a[33], UID0122 b[15]; usage text |
| `Y?` / year-unknown rows (grep by year finds nothing) | 8 | 12 | UID0027 a[8], UID0029 a[14]; see T6 |
| Date-keyed rows (MQ-1, PDO-3) unaddressable by `printings` | 7 | 14 | UID0215 a[21] etc. (nine calls), UID0225 b2[25]/[27]; see T12 |
| Hand-typed value lists miscounted or slipped | 7 | 8 | UID0007 c[20], UID0148 c[23]; see T17 |
| Harness dropped a tool's output (0 bytes, exit 0; reproduced fine offline) | 5 | 5 | UID0154 b2[13], UID0170 c[14], UID0112 b2[13], UID0220 a[18], UID0096 c[33] — a plumbing bug, not a tool bug |
| `find -printf` / `cat -A` unsupported on macOS | 4 | 6 | UID0030, UID0037, UID0122, UID0228 |
| `printings` SUMMARY `%g` rounding | 3 | 6 | UID0180 a[14], UID0135 a[21], UID0122 c[53]; see T11 |
| scipy/statsmodels missing | 2 | 5 | UID0165, UID0214 |
| `calc` result clipped at 2 KB | 2 | 3 | UID0028, UID0029 |
| `printings` scan capped at 400 parts, oldest first (a 1982 row unreachable) | 2 | 3 | UID0170 a[32]/c[25], UID0225 c[35] — scan newest-first when `--years` is absent |
| `--years` on find_tables hid the later re-based vintage | 1 | 3 | UID0007 (accidentally kept a right) |

## 5. Interactions and cautions

- **Vintage-rule questions are not tool-fixable in one direction.** UID0155 (first printing graded right), UID0148's reference (substituted an earlier vintage and was wrong), UID0204 (as-printed narrative figures right, the more precise FFO-1 cell wrong) all say the same thing: printings must expose both values with issue and flags (T10, T11, T14) and leave the choice to text/controls. T13's SUMMARY-first change is the one place an analysis says a right cell would flip (UID0155 b2), and only because that question's key contradicts the stated convention.
- **Print repairs in every view.** Two wrong cells (UID0028 b, UID0027 b) used `--raw` exclusively; a NOTES-only repair is invisible to them. Whatever T6-T9 add must also print above `RAW LINES:`.
- **The later_printing control inherits every printings blind spot** (UID0007 panel mixing, UID0062 title change, UID0133 untitled reprint): fixing T5/T10/T14 fixes the control too.
- **Cost order for a first v10 commit** (all low, all additive): T11 (`%g`), T15 (`--grep`), T12 (no-match diagnostics), T1 (rowlabels + prefix + OR), T2 (group-first listing), T10 (section/as-of keys + index warning), T16 (non-table line, part hint), T18 (calc helpers), the README items. Medium: T3, T6, T7, T8, T9, T13, T14, T4. Higher risk (touch the cached parser or the index build; diff titles over the whole corpus after): T5, T17's cross-table pass.
- **Evolvability.** T1-T3, T10-T13, T15-T16 are option/format changes an evolver can tune (thresholds, ranking weights, what a diagnosis line says); T5-T8 encode structural knowledge (sibling alignment, identity-based relabel) that should be shipped as fixed code with tests on the cited tables: UID0182 CM-I-1 2009-2011, UID0028/UID0029 AY-1 1970_06 vs 1970_05, UID0165 OFS-2 2007_03..2010_03, UID0148 MQ-1 1972_05/1973_05/1974_05, UID0007 the Dec 1949 / Feb 1950 summary tables, UID0225 MQ-1 1982_03:4373, UID0133 the 2013_12:3190 untitled reprint, UID0062 the 1948_03 title change, UID0114 MY-2's end in 2003_03.
