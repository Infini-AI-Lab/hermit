# W0 proposal: the v10 starting adapter (adversarial-critic cut)

Status: critic's verdicts on the three syntheses (`TOOLS.md`, `TEXT.md`, `CONTROLS.md`) plus the recommended W0 adapter
for `officeqa-v10-design/DESIGN.md` §4. Evidence: all 50 `q/*.json`, `index.json`, the digests named below, the v9 tool
source, and a replay of the cells' own `find_tables` queries against the pilot index
(`v9-p1-a/prepared/agent/tables/tables.sqlite`, 95,053 parts). No answer values appear in this file; tool-output
examples use `<v>` placeholders. Recount script: scratchpad `recount.py`; replay scripts: `emulate.py`, `emulate2.py`.

---

## 中文摘要（写给初学者）

**这份文件是什么。** 前面三份综合报告（工具 / 文字 / 控制）各自提出了一堆改动。我的任务是当"反方"：对最重要的 8 项改动，
逐一去 50 道题的记录里找反例（哪道答对的题恰好做了这项改动想禁止的事？改动后的输出会不会把它带偏？），用 python 重新数一遍
它们引用的数字，并判断改动是"真的提升能力"还是"把这 50 道题的答案特征偷偷写进去"（overfitting，过拟合：只对开发集有效）。
然后给出 v10 的起点适配器（W0）。

**几个词。**
- **adapter（适配器）**：harness（执行框架）之外的一切：给模型看的文字 `SEED.md`、工具代码 `tools/`、运行时控制 `controls.json`。
- **W0**：第 0 版适配器，五个实验组的共同起点；evolve 组从它开始学习。
- **parity（对等）**：harness 版和 prompt 版的 SEED 要含同样的知识，只是措辞不同（prompt 版的工作区没有四个表格工具，只有
  `officeqa.py`、`rg`、`sed`、`python`，所以它的 SEED 不能提工具名）。
- **exposure record（暴露记录）**：每项工具改动是从哪几道题设计出来的，其中哪些是 issue-47（官方答案有争议）的题。之后
  报告要在"全部 50 题"和"剔除这些题"两种口径下各算一遍。
- **FTS（全文索引）/ prefix（前缀匹配）/ OR fallback（退化为或）**：`find_tables` 的搜索方式：现在要每个词都精确出现在标题或表头；
  改动后也搜行标签、允许前缀（"dollar" 也匹配 "dollars"），一个都搜不到时退化为"部分词匹配"并按匹配词数排序。

**主要结论（细节见第 1 节）。**
1. 三份报告的**主数字全部复现**：修复层级 TOOL 17 / NONE 18 / TEXT 8 / CONTROL 7；87 个错格及其首次偏离分布；跑飞 a 9 题
   （占 a 全部输入 token 的 90%）、b 4、b2 1、c 0；a 对 b 错 12 题、b 对 a 错 1 题；b 有闸门回合的 27 题、b2 2 题。逐句文字
   裁决在 ±2 题内复现（同一条裁决串常带两个标签）。
2. **保留但修改**：`find_tables` 默认搜行标签 + 前缀（在真实索引上重放 13 条答对格的查询，排名一个都没变；UID0182 那条
   全错的查询里正确表升到第 3 名）；但报告里的"按命中词比例排序"**要删**——重放显示它把 7 条答对格的正确表往后推
   （UID0130 第 7 → 21，UID0135 第 23 → 265）。"搜不到就退化为 OR"只有在按命中词数排序时才有用（4 条零命中查询里正确表升到
   第 1/2/9/10 名），否则是 6 万条命中的洪水。
3. **删掉一句**：文字里"题目给出年份范围就等于钉住了版本，不要跑 printings"——这句只对 UID0007（issue-47）成立，会翻错
   UID0100、UID0135、UID0180（年份范围题，恰恰是"最新印次"救回来的）。
4. **改一句**：语料边界不能写"没有汇率序列"——语料里有 IFS-4/IFS-6 和 FCP 汇率表，UID0019 四格全靠它们答对。改成通用说法。
5. **闸门规则全部不进 W0**（设计已定 `gate:false`）；循环守卫按设计的旋钮保留，只改两处：同一命令被提醒后再重复要继续计数
   （UID0029 b 被提醒后又重复了约 136 次），提醒用完后硬阻断。跑飞的 42 个格里 13 个是答对的格，但它们的答案都在循环前算出，
   阻断不会伤害。
6. 留给学习器的东西（让 evolve 组有发挥空间）：单题专属的小抄（TABs 缩写、括号定义、编码答案、组头归属、同名表取第一张……）
   和几项只由一两道题支撑的工具改动（恒等式修复行、组头行、改名系列跟踪、组件表交叉引用）。

---

## 0. Recount of the syntheses' headline numbers (python over all 50 `q/*.json` + `index.json`)

| claim (file) | claimed | recount | verdict |
|---|---|---|---|
| smallest_change level (all three) | TOOL 17 / NONE 18 / TEXT 8 / CONTROL 7 | 17 / 18 / 8 / 7 | exact |
| wrong cells and first-divergence stages (TOOLS §1) | 87; INTERP 21, EXT 19, SRC_TABLE 15, CELL 11, VINTAGE 7, EXTR 5, FORMULA 4, OPER 3, ROUND 2 | identical | exact |
| runaways > 150 steps (CONTROLS §2.1) | a 9 (90 %), b 4 (78 %), b2 1 (45 %), c 0 | a 9 / 0.90, b 4 / 0.78, b2 1 / 0.45, c 0 | exact |
| a vs b pairwise (TEXT §4) | both right 19, both wrong 18, a-only 12, b-only 1 | 19 / 18 / 12 / 1 | exact |
| gate questions (CONTROLS §4.1) | b 27 of 50, b2 2 of 25 | 27; 2 (UID0007, UID0114) | exact |
| tool-call totals (TOOLS §1) | a 586/490/259/172/1852 ... | identical for a, b, b2, c | exact |
| "Loops" sentence (TEXT 0/13/0/37; CONTROLS 0/15/34) | — | kept 0, ignored 15 (13 once b-only loops UID0029/UID0225 are excluded), irrelevant 29-32 | supported; the two files differ only in the b exclusion |
| "Bulletins revise" misled 11 (TEXT 2.7) | 11 | 10 misled + 2 ignored (multi-label strings) | within 1 |
| "Several tables" misled 7 / "Alternatives" misled 4 | 7 / 4 | 6 / 4 | within 1 |
| `--grep` friction 37 q / 59 cells (TOOLS §4) | 37 / 59 | 41 q / 72 cells mention `--grep` in friction fields; "page rows 0-0" appears in 77 cell digests (39 right, 38 wrong) | conservative |
| `printings` no-match 24 q / 35 cells | 24 / 35 | "no matching row/column" appears in 49 cell digests (26 right, 23 wrong); 21 q / 31 cells name it as friction | supported |
| T1 "7 questions", T2 "9", T7 "6", T11 "3", T13 "9" | reading-based | keyword regex over `fix`: T1 17-21 loose / 8 as smallest, T2 7-8, T7 5-7, T11 2-3, T13 9-11 | the reading-based counts sit inside the regex ranges; none inflated |
| cells that saw `spurious extra label` (T7 harm test) | not stated | 8 right cells, 11 wrong cells | new evidence, see 1.5 |
| cells with one command run >= 4 times | not stated | 42 cells: 13 right, 29 wrong | new evidence, see 1.1 |

Everything the syntheses assert about counts is either exact or conservative. The disagreements below are about
inference from those counts, not the counts.

---

## 1. Verdicts on the top 8 proposed changes

Ranking key: questions whose fix names the change x wrong cells it claims to flip x whether the design already decided
it. Each verdict tries (1) to find right cells that did what the change prevents or that the changed output would
mislead, (2) recounts, (3) the overfitting test (does the change name a table, row, year or convention only these 50
questions need?).

### 1.1 Enforced loop guard, all harness arms; drop the "Loops" sentence (CONTROLS §2.4, §11) — KEEP-WITH-CHANGE

- Claim: advisory notes did not stop b's loops (21 notes, 4 runaways; UID0029 b repeated the noted command ~136 more
  times because `_loop_guard` skips commands already in `self.noted`, `controller_runtime.py:505`); a had no guard and
  spent 90 % of its tokens in 9 runaways; the text sentence scored 0 kept-and-helped.
- Refutation attempt: 13 **right** cells ran one command >= 4 times (UID0029 b 141x, UID0245 a 64x, UID0100 a 46x,
  UID0028 a 8x, UID0228 c 6x, UID0027 a 6x, UID0213 c 6x, UID0133 a 5x, UID0213 b 5x, UID0029 a 5x, UID0225 a 4x,
  UID0211 c 4x, UID0117 a 4x). Would a hard block have hurt them? UID0100 a computed its value at a[21] before the loop,
  UID0245 a at a[36], UID0029 b answered after its loop ended (CONTROLS §2.3); the 4-8x cases are short repeats that
  ended by themselves. A block that refuses re-execution but leaves the earlier output in context changes none of these
  answers. One residual: after a compaction the earlier output is gone, and "blocked: identical output already shown"
  then hides it (UID0029 b's compaction summary carried a wrong belief, b[347]).
- Recount: exact (see §0). Cycle detection is unnecessary: every runaway cycle in the pilot (3-14 commands) repeats each
  member command, so the per-command counter fires by the third cycle.
- Overfitting: none; domain-general.
- Verdict: KEEP the design's guard (`loop_threshold 3`, `loop_notes_max 3`, then the R6 hard block) with two changes:
  (i) remove the `self.noted` skip so a command already noted is counted again and re-noted every `loop_threshold`
  further repeats (UID0029 b, UID0114 b/b2, UID0188 b); (ii) the block message carries the first 20 lines of the cached
  output, so a post-compaction model is not left blind. DROP the "Loops" sentence from both SEEDs. Note for PREREG: the
  prompt arms have no controller (§1), so their cost is not comparable to the harness arms' (a's tokens were 8x c's).

### 1.2 `find_tables`: row labels by default, prefix tokens, OR fallback (TOOLS T1) — KEEP-WITH-CHANGE

- Claim: 7 questions; UID0182 b/b2/c never saw CM-I-1 because its title and header share no word with the question and
  the FTS is exact-token AND over title+header only.
- Refutation by replay (emulate2.py, pilot index, the cells' own queries). Row labels + prefix (`"tok"*` for tokens of
  4+ chars), AND, current ranking:
  - 13 right-cell queries: rank of the chosen table unchanged in 11 (UID0130 a 7, UID0224 10, UID0100 1, UID0154 1,
    UID0243 b 1, UID0007 a 5, UID0112 2, UID0109 1, UID0122 1, UID0228 1, UID0019 5); UID0180 2 -> 3 and UID0220 a[21]
    1 -> 2 (still on the first page); UID0135 a[5] 23 -> beyond `--limit 25` (that cell settled on CM-I-2 only at a[26]
    after five more searches, so the listing was not what found it). Group counts grow (UID0122 2 -> 50, UID0228 41 ->
    276, UID0154 4 -> 55): more noise below the target, never above it, because the where-score (title > header) still
    ranks first.
  - UID0182's failing query "Canadian dollar liabilities": CM-I-1 absent today -> rank 3 of 78 groups. This is the
    change's whole payload and it holds.
  - Zero-hit first queries (18 questions): prefix+rows alone rescues few (UID0170 -> rank 26, UID0077/UID0211/UID0182 b
    still no target). OR over content tokens ranked by number of tokens matched puts the target at rank 2 (UID0180),
    1 (UID0225), 9 (UID0243 c), 10 (UID0117); plain OR with today's ranking is a flood (60-86 k hits, 6-8 k groups)
    that shows the target in 3 of 8 cases.
- Overfitting: none (tokenizer/scope change). The synthesis's UID0182-specific expectation (CM-I-1 and CM-III-1 both
  visible) is met without any table-specific code.
- Verdict: KEEP rows-by-default + prefix; KEEP the OR fallback only in the ranked form (item W0-1 below); do NOT adopt
  the porter tokenizer (index rebuild for the same effect).

### 1.3 `find_tables`: rank by fraction of content tokens matched, print where they matched, mark snapshots (TOOLS T3) — DROP the ranking, KEEP the display

- Claim: 4 questions; UID0130 c's listing degenerated to group-size order because `where` needs the whole phrase.
- Refutation by replay: scoring groups by the fraction of content tokens found in title or any row label (as written)
  **demotes right cells**: UID0130 a/b2 7 -> 21, UID0135 23 -> 265, UID0243 b 1 -> 16, UID0228 1 -> 19, UID0019 5 -> 15,
  UID0007 5 -> 11, UID0220 a[21] 2 -> 7, UID0154 1 -> 3; it helps UID0224 (10 -> 2) and UID0182 (- -> 17). Cause: short
  row labels ("Total", "Sales") match everything, so dozens of groups tie at 1.0 and size decides. A milder variant
  (where-score first, token fraction as tie-break) keeps the right cells but pushes UID0182's CM-I-1 from 3 to 35,
  because the FCP tables match more tokens in their headers.
- Recount: 4 questions is right; the flip claimed for UID0130 c is one T=1 sample.
- Verdict: DROP token-fraction ranking. KEEP: print which query tokens matched and in which field
  (`match:tokens(rowlabels: canadian, dollar, liabilit)`), the `no title match for <words>` first line, and the snapshot
  tag on group lines (`snapshot as-of`), all display-only. The ranked OR fallback of 1.2 covers the zero-hit case.

### 1.4 `find_tables`: group-first listing, cut removes parts never groups, per-group cap under `--all` (TOOLS T2) — KEEP-WITH-CHANGE

- Claim: 9 questions; one 314-issue group ate the page on UID0180 b, the decisive summary table was cut on UID0243 a/c
  and UID0220 b2/c.
- Refutation attempt: the per-part `as-of <DATE>` tag is what solved UID0154 (all four cells, a[10]-[11], b[17],
  b2[8]-[9], c[11]-[12]) and UID0170 (a[23]-[24], b2[26], c[7]-[8]); both are issue-47/pinned-survey questions where the
  parts, not the groups, carry the decision. A listing that prints all group lines first and then runs out of budget
  before the parts would hide exactly those tags. In the pilot the tag was visible because the group was at rank 1-2 and
  its parts followed immediately.
- Recount: 7-8 questions by regex, 9 by reading; friction 21 q / 32 cells supported.
- Overfitting: none.
- Verdict: KEEP, with the change that parts are printed for groups in rank order until the budget is reached (so the
  top groups keep their as-of tags), the group line carries the row-period span and the as-of span
  (`as-of JAN 31 1977..OCT 31 1978`), `--all` caps one group at 12 parts with a continuation hint, and the cut trailer
  says how many groups were listed without parts. Merge the `Table N.` one-issue groups (UID0243 b[17] shows the split).

### 1.5 `show_table`: repair header shifts instead of warning (TOOLS T7) — KEEP-WITH-CHANGE (strict trigger, both mappings printed)

- Claim: 6 questions; UID0165 all four cells took the mislabelled column although the tool printed the spurious-label
  note and the identity in the same page (a[12]).
- Refutation attempt: the `spurious extra label` note fired in **8 right cells**: UID0225 a/b2/c (MQ-1: four trailing
  empty `Bank discount` labels, no ordinals), UID0130 a (TSO-1: `(no label)` c12, trailing unnumbered label), UID0154 b
  (OFS-2 1977: unnumbered labels, duplicated `Individuals > Total`), UID0112 b (FFO-2 2011_09: labels numbered (32),
  (33), (34) with the last column empty, and every identity c2=c3+c4 ... c11=c12+c13 holds **without** a shift),
  UID0148 a/c (MQ-1 again). An unconditional "drop the last label and shift" would have broken UID0112 b's column
  reading (and the identity a and c trusted there). The synthesis's trigger (k+1 labels, ordinals (1)..(k), exactly one
  unnumbered parent-only label, identity holds after the shift) fires on none of the eight: FFO-2's labels are all
  numbered and its identities hold unshifted; MQ-1/TSO-1/OFS-2 have no ordinals. The trigger must also require that the
  identity FAILS before the shift and holds after it, or the FFO-2 case (identity holds either way if a summed group is
  left of the empty column) becomes a coin flip.
- Recount: 5-7 questions by regex; friction 22 q / 40 cells supported.
- Overfitting: the OFS-2 2007_03..2010_03 test tables are development items; the rule itself is generic.
- Verdict: KEEP rule 1 with the stricter trigger (ordinals, one unnumbered label, identity fails before / holds after,
  trailing column empty in every row) and print **both** mappings (`v9 = c9 <label as printed>` and
  `LABELSHIFT-1 -> <label one to the left>`), never a silent relabel; KEEP rule 2 (amount-like label holding dates, with
  a sibling-borrowed label) since the `[date]` tag is already printed and the note only names the contradiction;
  DEFER rule 3 (identity-based Total relabel) to the learner: 4 alternatives, no cell flipped by it alone.

### 1.6 `printings`: key by panel/section/as-of, column-index warning, exact SUMMARY values, SUMMARY first (TOOLS T10, T11, T13) — KEEP-WITH-CHANGE

- Claim: T10 6+6 questions (three stacked panels give a "latest printing" from another panel: UID0007 b2[16] shows one
  (part, column, period) key with 7 "distinct values" that are three panels; two same-title tables merge into one
  REVISED block: UID0211 a[14]; four same-issue panels print as four printings: UID0044 a[20]; the ISOLATED detector
  called the correct column the outlier when the column index changed: UID0188 a[13]). T11 3 questions (`%g`: UID0180
  a[14] read a 6-digit mantissa in e-notation and lost 4 of 5 values). T13 9 questions (daily/weekly rows cut the
  SUMMARY: UID0224 a[12] lists 72 cells for one month).
- Refutation attempts:
  - T10 keys: right cells that *relied* on the merged key? The SUMMARY was decisive for 33 right cells; in all of
    them the table has one panel per period, so a section key changes nothing. Risk found instead: keying by the OCR'd
    section banner would split one series when the banner text varies between issues (the UID0062 title-split failure
    mode, false "same in all printings"). Mitigation in W0-3: key by a normalised panel stub, and when the same
    (column, period) exists under another stub in the same table, print `also printed under panel '<stub>' in N issues`.
  - T11 exact text: `printings` already prints `<v>.0` in one issue and `<v>` in the next for the same cell (UID0211
    a[14] listing shows both spellings of one value) so grouping on raw text would report false
    REVISED/DIFFERS everywhere. Group on the exact parsed value (Decimal of the stripped text), print the raw text of the
    first issue of each group.
  - T13 SUMMARY-first: UID0155 b2 (issue-47) is the one right cell an analysis says would flip: its truncated sweep hid
    the REVISED line and it took the first printing, which the disputed key follows. Confirmed in b2[12]/[16]. That is a
    property of a disputed key; the paper's stated convention is the latest revision, and UID0019/UID0100/UID0135/
    UID0180/UID0224 are right by it.
- Recount: supported (friction: false REVISED/cross-panel 14 q / 31 cells; printings cut 24 q / 40 cells; `%g` 3 q).
- Overfitting: none (keys and formatting).
- Verdict: KEEP T10 keys + column-index warning + snapshot line (W0-3), KEEP T11 with value-keyed grouping (W0-4), KEEP
  T13's SUMMARY-first reordering and month-key exactness (W0-11); DEFER the `--rows A..B` range mode to the learner
  (friction only, 40 lines, and it changes the observation parser's invocation grammar).

### 1.7 `printings` no-match diagnostics and `show_table --grep` semantics (TOOLS T12 + T15) — KEEP

- Claim: the two largest friction items (T15: `--grep` 0 rows in 37 q / 59 cells; T12: "no matching row/column" in 24 q
  / 35 cells) and the three longest hunts of the pilot (UID0114 a 3600 s timeout, b/b2 ~1400 s each) after a message
  that reads like a syntax error.
- Refutation attempt: could a diagnosis line mislead a right cell? The lines only replace an empty page or a generic
  message; 26 right cells saw "no matching row/column" and recovered by trying another spelling, which the diagnosis
  shortens. The only content-bearing addition is the end-of-series line (`<id> prints periods A..B; last issue printing
  it is <issue>`), computed from the index, plus an optional quote of a sentence containing "discontinu" near the table
  id in the next issue. That regex is generic, not UID0114's series.
- Recount: supported/conservative (§0).
- Overfitting: the examples in the synthesis name MY-2/MQ-1/PDO-3; the code does not.
- Verdict: KEEP all of T12 and T15 (W0-5, W0-6), including `--period FY2021` as an explicit filter with the month-count
  line (UID0102: b took 13 months; a and c 12).

### 1.8 TEXT: latest-printing rewrite with pin cases (TEXT 2.7 + N2 + N5) — KEEP-WITH-CHANGE (drop the "scope pins" clause)

- Claim: the sentence is the most valuable tool-backed item (decisive on UID0019, UID0100, UID0135, UID0180, UID0224)
  and misled 11 questions; the rewrite adds four pin cases and the OCR-not-revision rule.
- Refutation: the proposed clause "a table scope ('for the years 1940-1949') pins the source: do not run printings on
  it" is contradicted by three questions whose wording is a year range and whose right cells were right **because**
  they ran `printings`: UID0100 ("from FY 2007 - 2013 inclusive": four years revised, two vanish from later printings;
  4/4 right), UID0135 ("for the calendar years 1991 through 1996": the reference took the single issue and is graded
  wrong; 3/3 right), UID0180 ("at the end of the fiscal years 2005 to 2009": one year revised; b2 and c right by the
  latest printing). The clause fits only UID0007 (issue-47), where the real problem is a re-based later lineage (the
  Mar-1950+ printings widen the year span), which the group-line row span (W0-2) and the panel keys (W0-3) expose
  without a text rule. The named-issue / publication-date / quotation-date / "as of" pins are applied correctly by right
  cells on UID0057 a[32], UID0112, UID0154 a[14]/b2[14], UID0165, UID0204, UID0225 and cost nothing elsewhere.
  UID0062's "a phrase describing what a column includes pins nothing" rests on one question where all seven solvers and
  the reference are wrong and the key is inferred from an error bin: not SEED material (left to the learner).
- Recount: within 1 (§0).
- Overfitting: "for the years 1940-1949" is UID0007's wording verbatim; dropped.
- Verdict: KEEP-WITH-CHANGE: the two bullets in the SEED below (pins = named issue, publication date, quotation date,
  "as of" date; OCR variant = isolated last printing or a changed column index; "Preliminary"/snapshot = first
  printing), no scope clause, no column-phrase clause.

### 1.9 TEXT: entity = its own column, qualifier narrower than a total, table-choice rewrite (TEXT N3 + 2.10) — KEEP-WITH-CHANGE

- Claim: N3 fixes UID0109 b/b2 (summed a neighbouring "Other Defense, civil programs" column), UID0243 a (parent
  Total), UID0096 a/b/c (took the grand-total rows although the footnoted "Free list" component is excluded by the
  qualifier); 2.10's "prefer matching title and headers" misled 7 questions.
- Refutation attempt for N3: right cells that summed for a named entity: UID0149 c (summed the issues of an acronym's
  sub-block: the rule's "ONE row" would point at a sub-block header with no values, so it does not bite, but the
  sentence gives no help either), UID0220 a (the right answer is a *Total* column, because the question says "total":
  covered by the "unless the question says total" escape), UID0044 (a sum across panels of one row, not an entity
  column). No right cell took a single column where the key wanted a sum. The qualifier sentence rests on UID0096 alone
  (reference right, all cells wrong with the footnote on screen); kept short and generic.
- Refutation attempt for 2.10: "prefer the ROW or column label over the title" is contradicted by right cells that
  chose by title or section: UID0130 a/b2 (TSO-1 by title, a[8]/b2[8]), UID0135 a (by section, a[26]), UID0180 a/b2/c
  (OFS-1 by header wording, b2[8]). The evidence supports "row or column label, then title" only as the retry rule after
  a literal-title decoy (UID0102, UID0133, UID0182, UID0215). The proposed clause "a parenthetical such as '(U.S.
  Treasury)' names the publisher, so take the first table" is UID0211's wording and a benchmark convention: overfitting,
  dropped (left to the learner). The component-vs-summary tie-break is kept in generic form ("... - General" is the
  synthesis's example; the SEED says "a title naming one component").
- Recount: N3 5 questions (7 by regex as smallest); 2.10 verdict counts within 1.
- Verdict: KEEP N3 (both sentences, generic wording, with the "total/including/lists the parts" escape); KEEP 2.10 with
  the row/column-label preference stated as the retry after a decoy, the component/summary tie-break, the section-line
  tie-break, and the narrative-section clause; DROP the publisher/first-table clause.

### 1.10 Second-tier items (checked more lightly)

| item | verdict | evidence |
|---|---|---|
| T6 sibling year anchoring in every view incl. `--raw` | KEEP (W0-9/W0-10) | fires only with cell-for-cell agreement; SIBLINGS note seen in 12 right and 12 wrong cells; UID0028 b and UID0027 b used `--raw` and never saw the note (b[15]); no right cell could lose a printed year |
| T4 narrative section index | KEEP as optional W0-12 | UID0215 4/4 wrong, reference right; generic heading regex exists (`_is_caps_heading`); index-build change is W0-only (the learner cannot rebuild the index), so it is now or never for this round |
| T16 non-table line / part hint / `--long` hint | KEEP (W0-7) | 15 q / 19 cells snapped silently; `title_line` resolution must stay (UID0097 a[21], UID0155 b2[16]) |
| T5 index completeness (caption across prose, OCR titles, untitled twins) | DEFER | touches the cached parser; fixes 1 cell (UID0133 b); corpus-wide title diff needed |
| T8 identity-FAILS repair line | LEAVE TO LEARNER | designed from UID0007 only (issue-47) |
| T9 group headers / row footings | LEAVE TO LEARNER | UID0122 only; the reference calls the membership a residual ambiguity |
| T14 series across a title change + revision footnote | LEAVE TO LEARNER | UID0062 (all wrong, key inferred) + UID0133 |
| T17 component-of cross-table pass, row export | LEAVE TO LEARNER | UID0220 b2 only; 50 lines; crosses the calc boundary |
| T18 calc helpers | NOT POSSIBLE THIS ROUND | `calc.py` is hash-pinned (DESIGN §3) |
| T19 reference-series lookup | DROP | every analysis says it would not change the grade; breaks the closed-corpus premise |
| N1 corpus boundary | KEEP-WITH-CHANGE | the synthesis's list ("no CPI, GDP, exchange-rate or population series") is false for exchange rates: IFS-4/IFS-6 "Weighted-Average of Exchange Rate Changes" tables exist in ~100 issues and the FCP tables carry rates that solved UID0019 (4/4 right). Generic wording in the SEED |
| N4 answer form | KEEP (generic half) | UID0245 (a right by default, c `scale_10^2`), UID0096 a; UID0237 is issue-47: if its key flipped to the dollar gap the metric sentence would turn a wrong |
| N6 parenthetical definition, N9 encoded answers, N10 TABs glossary, N12 group headers, N13 date windows, N14 one-table ratios, N16 regression index, N17 stacked panels, N20 paraphrased columns | LEAVE TO LEARNER | each from one question; N10 names a specific table and sub-block (officeqa_specific) |
| gate rules (10) | DROP for this round | DESIGN gate:false; 75 mechanism runs, 1 wrong->right, 2 right->wrong; `scale`/`recompute_final` recorded as candidates for a future `gate: true` adapter |
| ledger splice | KEEP as is | 5 uses, 0 grade effect; re-keying by question nouns deferred |
| 8000-byte cap | KEEP; what survives changes via W0-2/W0-11 | 41/50 questions cite cuts |
| empty-output guard | ADD if cheap (new knob) | 5 capture losses, 0 grade effect |
| T = 0 | KEEP | c -7 on one-step judgments |

---

## (a) Harness `SEED.md` (4065 bytes)

```markdown
# Working advice

- Compute every final number with `calc`, not in your head, and answer with the value it printed, rounded as the question asks.
- Provenance: use numbers a tool printed. Before you rely on a value, print the line it comes from, so the number in your calculation is the number in the source. For a table printed in year blocks, use the block's printed Total row.
- Answer count and format: give exactly as many values as the question asks for, in its order and format (a bracketed list when it asks for one). The final line is a number or a list, never a sentence; if a value cannot be found, answer your best documented number.
- Answer form: the metric the question names fixes what you report (a normalized difference, share, rate or change is that metric, not the raw difference). Multiply by 100 only when the answer clause says percent, percentage or %; a rate or ratio with no percent cue is a decimal. Input units ("in percent") do not set the output form.
- Alternatives: when two candidates fit (tables, sections, rows, printings, readings), name both, compute both with calc, and answer the one the question's wording selects; never average. Candidates include other tables and narrative sections, not only neighbouring columns; more decimals is not a reason.
- Corpus boundary: the corpus is Treasury Bulletins only. A series that is not a Bulletin table (a price index, a market price, a chart's plotted bars) is not in it: one `find_tables` and one `rg` settle that; then stop searching, state your assumption and answer.

OfficeQA table notes:
- Bulletins revise earlier numbers. Unless the question names an issue, publication date, quotation date or "as of" date, use the latest printing of each value: read the `printings` SUMMARY line for every value that enters your answer. When it names one, use each figure as that issue prints it; do not replace it with a later revision.
- In `printings`, a value that only the last printing shows while earlier printings agree, or a value whose column index differs from the other issues, is an OCR variant, not a revision. A table titled "Preliminary" or a one-date snapshot is a first printing; the revised value is in the section's time-series table.
- Match the period exactly: FY and CY rows with the same year number are different rows; "est." and "to date" rows are not the year's row; a fiscal year has exactly 12 month rows. Check the filled period of every cell you use.
- Read `show_table`'s NOTES on every page. A row flagged "identity FAILS", "Y?" or repaired: compare it with other printings and take the value most printings agree on that restores the row total; for "Y?" rows open the SIBLINGS printing to read the years. A header flagged shifted or spurious, or a column listed under different indexes across printings: map the numbered labels (1)..(n) onto the n numeric columns in order and confirm with the identity line before reading a column by its label.
- Finding the table: search `find_tables` with the question's key nouns; on 0 groups or no good match retry with fewer words. Prefer the table whose row or column label is the question's noun phrase, then the title. A title naming one component ("... - General") is not the total: for "total X" find the summary table with an "X > Total" column. Two tables with one title: tell them apart by `show_table`'s section line. A question naming a report or section may mean narrative text: `rg -n -i` the phrase and read that section.
- Entity = its own column: an agency, program or item the question names is the ONE column (or row) whose label names it, not a parent Total beside it and not a neighbouring "Other" column, unless the question says "total", "including" or lists the parts. Words that repeat the table title or units line describe the table, not extra columns. A qualifier narrower than a total row ("subject to duty" vs "dutiable and taxable") means the footnoted component it excludes is subtracted from every operand: compute both readings and answer the one that honours every word.
```

## (b) Prompt `SEED.md` (4091 bytes; names no tool; `check_w0` substring test passes: no `find_tables`, `show_table`, `printings`, `calc`)

```markdown
# Working advice

- Compute every final number with `python`, not in your head, and answer with the value it printed, rounded as the question asks.
- Provenance: use numbers you have seen in the corpus text. Before you rely on a value, print the line it comes from with `sed -n`, so the number you compute with is the number in the source. For a table printed in year blocks, use the block's printed Total row.
- Answer count and format: give exactly as many values as the question asks for, in its order and format (a bracketed list when it asks for one). The final line is a number or a list, never a sentence; if a value cannot be found, answer your best documented number.
- Answer form: the metric the question names fixes what you report (a normalized difference, share, rate or change is that metric, not the raw difference). Multiply by 100 only when the answer clause says percent, percentage or %; a rate or ratio with no percent cue is a decimal. Input units ("in percent") do not set the output form.
- Alternatives: when two candidates fit (tables, sections, rows, issues, readings), name both, compute both with python, and answer the one the question's wording selects; never average. Candidates include other tables and narrative sections, not only neighbouring columns; more decimals is not a reason.
- Corpus boundary: the corpus is Treasury Bulletins only. A series that is not a Bulletin table (a price index, a market price, a chart's plotted bars) is not in it: one `officeqa.py` search and one `rg` settle that; then stop searching, state your assumption and answer.

OfficeQA table notes:
- Bulletins revise earlier numbers. Unless the question names an issue, publication date, quotation date or "as of" date, use the latest printed value: `rg -n` the table title or id across all issues, open that row in the latest issues printing it, and take the latest value. When it names one, use each figure as that issue prints it; do not replace it with a later revision.
- A value that only the last issue shows while earlier issues agree, or a value that sits in a different column position than in the other issues, is an OCR variant, not a revision. A table titled "Preliminary" or a one-date snapshot is a first print; the revised value is in the section's time-series table.
- Match the period exactly: FY and CY rows with the same year number are different rows; "est." and "to date" rows are not the year's row; a fiscal year has exactly 12 month rows. Check the year banner above every row you use.
- Check every row you use: if its cells do not sum to the printed total, or its year is not printed, compare it with the same table in a neighbouring issue and take the value most issues agree on that restores the row total; read missing years from that issue's header. If the header has more labels than numeric columns, map the numbered labels (1)..(n) onto the n numeric columns in order and confirm with a row total before reading a column by its label.
- Finding the table: `rg -n -i` the question's key nouns over the corpus; on no hits retry with fewer words and with row-label words. Prefer the table whose row or column label is the question's noun phrase, then the title. A title naming one component ("... - General") is not the total: for "total X" find the summary table with an "X > Total" column. Two tables with one title: tell them apart by the section banner above them. A question naming a report or section may mean narrative text: `rg -n -i` the phrase and read that section.
- Entity = its own column: an agency, program or item the question names is the ONE column (or row) whose label names it, not a parent Total beside it and not a neighbouring "Other" column, unless the question says "total", "including" or lists the parts. Words that repeat the table title or units line describe the table, not extra columns. A qualifier narrower than a total row ("subject to duty" vs "dutiable and taxable") means the footnoted component it excludes is subtracted from every operand: compute both readings and answer the one that honours every word.
```

## (c) Parity checklist (`packs/officeqa/SEED_KNOWLEDGE`; `check_w0` asserts each sentence is present verbatim)

| id | knowledge item | harness SEED sentence | prompt SEED sentence |
|---|---|---|---|
| K1 | compute with code | "Compute every final number with `calc`, not in your head" | "Compute every final number with `python`, not in your head" |
| K2 | provenance: print the source line | "print the line it comes from, so the number in your calculation is the number in the source" | "print the line it comes from with `sed -n`, so the number you compute with is the number in the source" |
| K3 | answer count and format | "The final line is a number or a list, never a sentence" | same |
| K4 | percent only on cue; named metric fixes the form | "Multiply by 100 only when the answer clause says percent, percentage or %" | same |
| K5 | alternatives: both computed, wording decides, never average | "never average" (+ "Candidates include other tables and narrative sections") | same |
| K6 | latest printing unless pinned | "Unless the question names an issue, publication date, quotation date or \"as of\" date" | same |
| K7 | OCR variant vs revision; preliminary/snapshot = first printing | "is an OCR variant, not a revision" | same |
| K8 | FY vs CY; est/to-date; 12 month rows | "a fiscal year has exactly 12 month rows" | same |
| K9 | repaired / identity-fail / Y? rows; shifted headers | "map the numbered labels (1)..(n) onto the n numeric columns in order" | same |
| K10 | table choice: nouns, retry, component vs summary, same title -> section | "for \"total X\" find the summary table with an \"X > Total\" column" | same |
| K11 | entity = own column; qualifier narrower than total | "Entity = its own column" | same |
| K12 | corpus boundary | "Corpus boundary: the corpus is Treasury Bulletins only" | same |
| K13 | narrative sections | "may mean narrative text" | same |

Byte parity: harness 4065 B, prompt 4091 B (difference 1 %). Model-visible bytes differ by `tools/USAGE.md`
(harness only, <= 3,072 B) and the loop templates; PREREG records both totals. Dropped from both: the "Loops" sentence
(control), the FY-definition clause (never cited), the "For a sum over the columns of one row" item (replaced by K11).

## (d) Tool changes: ordered implementation list with exposure record

Issue-47 items are marked `*` (UID0007, UID0154, UID0155, UID0213, UID0237). Every change is additive: old lines stay,
new lines are inserted, so the frozen observation parser's header recovery still works. All examples use `<v>` for
values. Ordered by cost x cells addressed; W0-1..W0-8 are the minimum commit, W0-9..W0-12 follow if the build budget
allows.

**W0-1 `find_tables` scope, prefix, ranked OR fallback** (`otables.find_page`, `_fts_phrase`; ~40 lines).
- Default FTS scope `{title header rowlabels}`; `--rows` stays accepted (no-op, help says "(default)"); new
  `--title-only` restores the old scope. Tokens of 4+ characters become prefix terms (`"canadian"*`).
- When the AND query returns 0 parts: retry with OR over content tokens (drop stopwords and 1-letter tokens), rank groups
  by number of distinct query tokens matched (a title/header match counts before a row-label-only match at equal
  count), keep the existing where/kind ranking inside equal counts, print at most `--limit` groups, first line
  `no table has all 6 words; best partial matches (matched k of 6):`.
- Every group line gets the matched tokens and field: `| match:tokens(rowlabels: canadian, dollar, liabilit)` (computed
  by one MATCH per token on `{title header}` and on `{rowlabels}`; no schema change); when no token matches any
  title/header: `no title match for <words>; row-label matches only:`.
- Exposure: UID0182, UID0029, UID0133, UID0180, UID0218, UID0237*, UID0149; zero-hit evidence UID0225, UID0117, UID0211,
  UID0170, UID0077, UID0243, UID0097, UID0224, UID0245.

**W0-2 `find_tables` group-first listing** (`find_page` listing loop; ~40 lines).
- Print group lines for every ranked group up to `--limit` first:
  `[FD-1] <title> | 317 issues 1969_10..2025_09 | rows FY1954..2025-Aug | as-of - | match:title`.
- Then parts per group in rank order (latest 3, or all with `--all`, capped at 12 per group with
  `    ... 302 more parts (add --years)`), stopping at the budget; the trailer reads
  `... OUTPUT CUT at 8000 bytes: 25 groups listed, parts shown for the first 6; narrow with --years or --limit`.
- Merge groups whose normalised title differs only by a leading `Table N.`; `[snapshot]` tag on groups whose parts carry
  an as-of date and no period column.
- Exposure: UID0243, UID0180, UID0133, UID0135, UID0094, UID0007*, UID0154*, UID0237*, UID0149, UID0220, UID0130.

**W0-3 `printings` keys, column-index warning, snapshot line** (`printings_data`/`printings_page` lines 2109-2227,
`latest_printing` 2233; ~40 lines).
- SUMMARY grouped by `(part, panel stub, column key, period, asof)`; header line
  `[CY1942] part1 panel 'Budget expenditures' col (3) (Mar.): 12 printings, 2 distinct values - REVISED/DIFFERS`;
  new option `--section <substring>` filters panels/sections; when the same (column, period) also exists under another
  stub: `also printed under panel 'Budget receipts' in 12 issues (not merged)`.
- Before the SUMMARY, when the matched column sits at different indexes across issues:
  `column index differs across issues (c7: 7 issues; c8: 3 issues): the minority may be a mis-parsed header; verify with show_table --long`,
  and the ISOLATED line is suppressed for printings whose column index is the minority.
- Snapshot tables: `snapshot table (as-of in title, no period column): one printing per as-of date; keyed by as-of`.
- Exposure: UID0007*, UID0044, UID0170, UID0211, UID0130, UID0220, UID0188, UID0228, UID0165, UID0214, UID0218.

**W0-4 `printings` exact SUMMARY values** (lines 2208, 2213; ~8 lines).
- Distinct-value groups keyed by the exact parsed value (`Decimal` of the raw text with commas, r/p flags and footnote
  marks stripped), printed as the raw text of the group's first issue: `      <raw as printed>: 2006_12..2011_09 (20)`;
  no `%g`, no e-notation.
- Exposure: UID0180, UID0135, UID0122.

**W0-5 `printings` no-match diagnostics and label normalisation** (line 2185; ~40 lines).
- Period outside the printed span: `<id> prints periods 1991-01 .. 2002-12; the last issue printing it is 2003_03 (<file:line>); later issues do not print <id>`,
  plus, when a sentence containing "discontinu" occurs within 50 lines of the id in the next issue, one quoted line.
- `--col` miss: `no column matches '<col>'; columns of <file:line> are: c1 ... | c4 ... | ...` (footnote marks stripped on
  both sides). Row window: `rows FY2011..FY2014 were last printed in 2016_09; --years 2020-2025 starts after that`.
  Periods as columns: `this table has periods as columns: use --col "<period label>" --row <label>`. Date-keyed rows:
  `rows of <id> are keyed by c2 <date column>; pass that date as --row`. Intra-word spaces deleted before matching
  (`To tal as sets`).
- Exposure: UID0114, UID0218, UID0057, UID0097, UID0122, UID0148, UID0109, UID0180, UID0225, UID0170, UID0101.

**W0-6 `show_table --grep` semantics and empty pages** (lines 1984-1992; ~30 lines).
- 0 rows: `0 rows: --grep is a case-insensitive substring of the filled period (1942-12), the raw row label or the section, not a regex and not a column filter (use --match for columns); '<g>' matches column c2 <label>` when it matches a column label.
- `|` or repeated `--grep` = OR; month-name forms (`1969-Nov`, `Nov 1969`) normalised through `parse_query_period`;
  new `--period FY2021|CY1996` keeps rows whose filled period falls in it and prints
  `FY2021 = 2020-10..2021-09: 12 rows kept (excluded: 2020-09)`; `--from-row 12 is past the last filtered row (12)`.
- Exposure: UID0170, UID0224, UID0237*, UID0100, UID0112, UID0102, UID0057, UID0149, UID0214, UID0220, UID0182, UID0007*,
  UID0245, UID0109.

**W0-7 `show_table` non-table lines, parts, `--long` hint** (`table_at` line 501; ~20 lines).
- `no table starts at <file>:830; showing the next table at :835 (<title>); nearby: :875 <title>`;
  `line 1295 is row 15 of TABLE MS-1 starting at 1281`; for a chart caption: `figure caption at :LINE; plotted values are not in the text corpus`;
  `this part has N rows; the table continues in part 2 at :LINE (use --stitch)`; after a cut `--long` page:
  `these N rows fit one page without --long, or with --cols`; usage text says `--from-row` is 0-based.
- Exposure: UID0094, UID0037, UID0214, UID0154*, UID0029, UID0133, UID0148, UID0112, UID0077, UID0211, UID0225.

**W0-8 `show_table` header-shift repair, strict** (branch ~line 1150 and the label/type block ~1220; ~35 lines).
- Rule 1 fires only when: the header has k+1 labels for k data-bearing columns, the labels carry ordinals (1)..(k) with
  exactly one unnumbered parent-only label, the trailing labelled column is empty in every row, and the total identity
  FAILS with the printed labels and holds after shifting the labels right of the unnumbered one by one column. Output in
  COLUMNS: `v9   = c9 <label as printed> (9) [num]  LABELSHIFT-1 -> <label one to the left>`; NOTES:
  `header realigned: 13 labels for 12 numeric columns; identity c3 = sum c4..c12 holds only after the shift; sibling <file:line> agrees`.
  Otherwise the existing note stays.
- Rule 2: an amount-like label on a `[date]` column with a pandas `.1` suffix whose base sits on a num column:
  `c2 <label> [date] (label repaired from sibling <file:line>: <sibling label>)`.
- Exposure: UID0165, UID0148, UID0213*, UID0214, UID0077, UID0228, UID0102, UID0243, UID0101, UID0225; harm-tested on
  UID0112 b, UID0130 a, UID0154 b, UID0225 a/b2/c, UID0148 a/c (no fire).

**W0-9 NOTES in `--raw`** (`show_page` lines 1887-1906; ~6 lines): print `note_lines` above `RAW LINES:`.
- Exposure: UID0028, UID0027, UID0225, UID0148.

**W0-10 sibling year anchoring** (`analyze`/`_panels_for`/`_fill`; ~70 lines).
- When a column-group panel has no printed year, take the nearest sibling (same id, same width) with year labels, align
  by exact agreement of one full row block per panel, adopt its `year0` per group, fill periods, and print
  `YEARS ANCHORED from <file:line> (240/240 cells agree): panel1 = 1959,1960,1961; ...; panel4 = 1968,1969,1970 (partial, ends 1970-05)`;
  rows read `1964-02 <anchored>`; otherwise `no sibling aligns - years unknown`. `printings --row 1964-02` then resolves.
- Exposure: UID0028, UID0029, UID0027, UID0117 (the years in the example are the synthesis's; the code carries none).

**W0-11 `printings` SUMMARY first, month keys exact** (~15 lines).
- Build SUMMARY lines first and reserve their bytes in `_fit_lines`; a month `--row` matches only rows whose filled
  period is that month unless `--all-periods`; listing trailer `... N more listing lines omitted; SUMMARY complete`.
- Exposure: UID0224, UID0062, UID0077, UID0155* (the one right cell that would flip), UID0135, UID0114, UID0122, UID0243,
  UID0101.

**W0-12 (optional, W0-only) narrative section index** (`build_index` + schema + `INDEX_VERSION`; ~80 lines).
- Index upper-case headings and sub-headings; `find_page` prints a `[section]` block above table groups when >= 2
  content tokens match a heading: `[section] TREASURY FINANCING OPERATIONS, OCTOBER-DECEMBER 1987 | treasury_bulletin_1988_03.txt:1688-1800 | sub-headings: Auction of 2-Year Notes (:1692) ...`;
  with 0 table parts: `no table matched; N narrative sections match these words (read with sed -n)`. No new tool name.
- Exposure: UID0215, UID0133, UID0183, UID0204, UID0037.

**USAGE.md** (<= 3,072 B; every line names a manifest tool or an option): the README block of v9 plus one line each for:
`find_tables` searches title, header and row labels with prefix matching, one query per call; `--title-only`;
`show_table --grep` is a substring over label/period/section, `--match` filters columns, `--period FY2021`;
`--from-row` is 0-based, pass the number the continuation line prints; `printings --row` takes the row period or
label and `--col` the column (swap them for tables with periods as columns), `--section`; quote multi-word values;
`printings` has no `--all`, `show_table` takes `<file> <line>`; `calc` prints several conventions in one run. The
non-tool facts (absolute corpus path for `rg`/`sed`, `docs.search(...)["results"]`, no `find -printf`/`cat -A`,
`statistics.NormalDist` instead of scipy) go in the base README's environment section, which both architectures see.

**Tests pinned to development items** (so a later reviewer knows they are not held-out): UID0182 CM-I-1 2009-2011
(W0-1), UID0028/UID0029 AY-1 1970_06 vs 1970_05 (W0-10), UID0165 OFS-2 2007_03..2010_03 (W0-8), UID0148 MQ-1
1972_05/1974_05 (W0-8 rule 2), UID0007* the Dec 1949 / Feb 1950 summary tables (W0-3), UID0114 MY-2's end in 2003_03
(W0-5), UID0243 the 1962 `Table 1.` groups (W0-2).

## (e) Controls (`controls.json`, harness adapters)

`{"loop_guard": true, "loop_threshold": 3, "loop_notes_max": 3, "splice": true, "ledger_splice_bytes": 8000, "gate": false, "enforce_requery_max": 0}` (the design's values), with:

| control | keep/drop | change |
|---|---|---|
| loop guard | KEEP | remove the `self.noted` skip (`controller_runtime.py:505`): a noted command is re-noted every `loop_threshold` further repeats; after `loop_notes_max` notes the R6 hard block refuses any command whose text + output hash repeats a previous call and prints `blocked: identical output already shown (call N, k repeats)` followed by the first 20 lines of that output |
| "Loops" advice sentence | DROP | 0 kept-and-helped in 50 analyses |
| gate-off loop templates | KEEP (design) | no `submit` mention; "end your turn with the value you trust, or run a command you have not run yet" |
| ledger splice after compaction | KEEP | selection change deferred (0 grade effect) |
| submit gate and its 10 rules | DROP this round (`gate: false`) | `scale` (with the calc-source 100/200 check) and `recompute_final` are the only candidates for a future `gate: true` adapter; `competing_candidates`, `later_printing`, `flagged_cells`, `table_alternatives`, `row_coverage`, `period_match` never |
| 8000-byte cap | KEEP | what survives a cut is W0-2 / W0-11 |
| empty-output guard | ADD if the broker change is cheap | new knob `empty_output_retry: 1`: a tool command with exit 0 and 0 bytes is re-run once, then `[harness] <cmd> printed nothing (exit 0); output lost, run it again` |
| temperature | KEEP T = 0 | |
| 3600 s budget | KEEP | |

## (f) Deliberately left for the learner (room for the evolve arms)

Notes (scoped, one question each in the pilot): parenthetical-definition rule (UID0101), encoded answers with four-digit
years (UID0027), acronym glossary "TABs" and the by-issues sub-block (UID0149), group-header membership (UID0122),
same-title tables: publisher parenthetical / first-table convention (UID0211), "a column-description phrase is not a
date pin" (UID0062), one-table ratios (UID0182), regression index origin (UID0100), stacked-panel sums (UID0044),
paraphrased column headers (UID0218), date-window row counts (UID0148), rate/annualisation conventions (UID0230,
UID0240), percent-difference convention (UID0220).

Tool revisions (code the learner can patch; none touch the index): identity-FAILS repair line with the cross-printing
majority (T8), group-header lines and row footings in `--long`/ROWS (T9), series continuation across a title change and
the revision footnote next to `r` values (T14), component-of cross-table note (T17), `printings --rows A..B` /
repeated `--col` range mode and the compact per-issue listing (T13 remainder), identity-based Total relabel (T7 rule 3),
snapshot-aware `printings` beyond the note, `show_table --compare <issue>`, `find_tables` ranking weights and
`--limit` defaults, the thresholds in W0-1/W0-2/W0-8 (all plain constants).

Not learnable this round and not in W0: T5 index completeness, T18 calc helpers (calc pinned), T19 series lookup.

## (g) Open risks

1. **Index changes are W0-only.** `tables.sqlite` is built at prepare time; the learner patches `tools/*.py` but never
   rebuilds the index, so W0-12 (and T5) either ship now or not at all this round. If W0-12 is skipped, UID0215-type
   questions stay unreachable for every arm, which is fair but wastes 4 known-wrong cells.
2. **Row-label default raises group counts** (UID0122 2 -> 50, UID0228 41 -> 276, UID0154 4 -> 55 on the replay). The
   target's rank held in every right-cell query tested, but the page below it is noisier; W0-2 keeps the group lines
   readable. The replay covered 28 queries, not the 1,795 the cells ran.
3. **OR fallback floods** (60-86 k hits): only the ranked, capped form is safe; an unranked fallback would be worse than
   the empty page.
4. **Panel keys can split a series** when the OCR'd banner varies between issues (false "same in all printings", the
   UID0062 failure mode moved to panels); the `also printed under` line is the mitigation, untested on the corpus.
5. **Hard block after compaction** hides output the model no longer has; the 20-line echo is a partial remedy.
6. **No format check without a gate**: UID0114 c answered a sentence, UID0245 c multiplied by 100 inside calc; only the
   base COMMON instructions and K3/K4 guard this now.
7. **Text sentences with single-question support** (K11's qualifier clause from UID0096; K4's metric clause touching
   UID0237*) may cost a question whose key follows the other reading; both are worded generically and are the learner's
   to refine.
8. **Model-visible bytes differ by architecture** (USAGE.md + loop templates on the harness side); SEED parity is 1 %,
   but PREREG must report totals, and any harness-vs-prompt gap partly measures those bytes.
9. **Development-set exposure**: every W0 change above was designed from the 50 questions; §7's "excluding the targeted
   items" contrast is the only guard, and the issue-47 items (UID0007, UID0154, UID0155, UID0213, UID0237) plus the 11
   irreducible questions (CONTROLS §9) should be down-weighted in fitness.
10. **T = 0 is not deterministic across forks** (baseline-variance audit): single-question effects of any W0 item are
    noise; judge W0 on the aggregate and on the friction counts (cuts, zero-hit searches, no-match messages, loops).
