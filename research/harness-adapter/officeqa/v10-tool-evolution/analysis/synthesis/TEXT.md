# v10 synthesis, lens TEXT: what each sentence of cell a's AGENTS.md is worth, item by item

Sources: all **50** per-question analyses in `q/*.json` (count verified: 50 files, 50 distinct task ids,
ordinals 1-50), `index.json` for correctness and run stats, and cell a's `workspace/AGENTS.md`
(`v9-p1-a/runs/episodes/001/workspace/AGENTS.md`; the 11 advice items are quoted in section 2). This file
replaces the earlier partial version (26 of 50). No answer values appear in this file.

---

## 0. 中文摘要（写给初学者）

**这份文件在回答什么？** 我们给模型（GLM-5.3-flash）一段约 2.5 KB 的"工作建议"文字，放在 `AGENTS.md`
里（AGENTS.md = 模型开始做题前读到的说明文件，相当于"新员工须知"）。现在要逐句判断这段文字有没有用：
模型照做并因此答对（**kept-and-helped**）、没理会（**ignored**）、照做反而答错或浪费步数（**misled**）、
与这题无关（**irrelevant**）。然后决定每句 **保留 / 改写 / 删除**，并列出 50 份分析里要求 **新增** 的句子，
最后比较"有文字"与"无文字"两组的成绩，估算文字到底值几题。

**术语（第一次出现时解释）**
- **cell（实验组）**：同一批 50 道 OfficeQA 题，用不同配置各跑一遍。**a** = 有建议文字，温度 0
  （temperature 0 = 模型每次都选最可能的词，结果可复现），31/50 对；**b** = 没有建议文字，但在"提交答案"时
  由程序执行 10 条检查规则（这层叫 **gate**，提交门禁），20/50；**b2** = a 的文字 + 5 条规则，只跑了前 25 题，
  13/25（a 在同样 25 题上 15/25）；**c** = 和 a 一样的文字，但温度 1.0（带随机采样），24/50。
- **harness adapter（线束适配器）**：把模型和环境接起来的那层。它拥有三样东西：给模型看的 **文字**（text）、
  **工具**（tools：`find_tables` 搜表、`show_table` 打印一张表、`printings` 列出同一格子在各期刊物的历次印次、
  `calc` 算数）、和运行时 **控制**（controls：循环守卫、提交检查）。本文只看 **文字** 这一样；工具和控制
  分别见 `TOOLS.md` 和 `CONTROLS.md`。
- **printings（印次）/ vintage（版本）**：财政公报（Treasury Bulletin）每期会重印并修订往期数字，同一个格子
  在不同期刊物上可能是不同的值。"latest printing" = 最新一期印的值；"pinned issue" = 题目点名了某一期或
  某个日期，那就只能用那一期。
- **OCR**：光学字符识别。语料是扫描件转成的文本，表头会错位、年份会丢、数字会错，很多"看起来像修订"的差异
  其实是 OCR 错误。
- **first divergence（首次偏离）**：一条错误轨迹里第一次与正确解法分岔的那一步（选错表、选错列、公式错、
  算术错、外部知识错……）。
- **issue47**：这道题的官方答案或题面被公开质疑过；这类题不能拿来衡量适配器好坏。
- **strict**：严格判分，答案字符串必须与标准答案完全一致。

**一句话结论：** 这段文字的毛效果约 **+5 题 / 50 题**（a 比 b 多对 12 题，其中分析归功于文字的有 5 题），
但 **稳健** 效果只有 **+1 到 +3 题**：5 题里有 4 题，另一个拿着完全相同文字的 cell 也答错了；
文字还害了 a 一题（UID0243）。同一段文字在 a 和 b2 之间来回摆动 3-5 题，即文字效果 **小于两次运行之间的
随机波动**。几乎全部价值来自 3 句话：用 calc 算、先打印来源行再用数、"最新印次"规则（这句既是最有用的，
也是最容易带偏的）。

**逐句裁决（按题数计，一题可同时落在两个标签；详见第 2 节）**

| 句子 | 帮到 / 无视 / 带偏 / 无关 | 决定 |
|---|---|---|
| Loops（别重复同样命令） | 0 / 13 / 0 / 37 | **删除**，改为运行时循环守卫（control） |
| Compute with calc（用 calc 算） | 48 / 1 / 1 / 1 | **原样保留** |
| Provenance（先打印来源行） | 36 / 4 / 2 / 8 | **保留**，加"区块表用印出的 Total 行" |
| Answer count and format（答案个数与格式） | 14 / 1 / 0 / 35 | **保留**，加"最后一行永远是数，不是句子" |
| Percent or fraction（百分数还是小数） | 11 / 3 / 0 / 36 | **改写**为"题目点名的指标决定答案形式" |
| Alternatives（两个候选按措辞取舍） | 21 / 7 / 4(+4) / 22 | **改写**：候选集要包含别的表和叙述段；多几位小数不是理由 |
| Latest printing（用最新印次） | 33 / 2 / 11 / 9 | **改写**：写清四种"钉住"情形；"末期孤立值/列序不同"是 OCR 不是修订 |
| Match the period（FY/CY 期别） | 18 / 1 / 0 / 31 | **保留**，加"一个财年正好 12 个月行" |
| show_table 修复标记 | 8 / 3 / 0 / 38 | **改写**：说清对比之后取什么值；扩展到表头/列标签错位 |
| Several tables look alike（同名表按标题匹配） | 30 / 9 / 7 / 4 | **改写**：先按行标签再按标题；组件表 vs 汇总表；同名表看 section；可能是叙述段 |
| Sum over columns（按列求和） | 1 / 0 / 1 / 48 | **替换**为"题目点名的实体 = 自己那一列" |

**要新增的句子（按涉及题数排序，详见第 3 节）：** 语料库边界——CPI/汇率等外部序列和图表数值不存在，
查一次就停（11 题，全是省步数）；钉住的期刊/日期/范围不要跑 printings（8 题，并入"最新印次"改写）；
实体 = 自己那一列、限定词比总计行窄要扣掉脚注项（5 题，正确性）；答案形式由题目点名的指标决定（3 题）；
printings 里的末期孤立值、列序不同、"Preliminary"表是 OCR/初印不是修订（3 题）；定义括号就是公式（1 题，但
修 3 个 cell）；其余各 1 题（编码答案四位年份、缩略语 TABs、叙述性章节、组头归属、日期窗口、比值同表同单位、
回归下标、堆叠面板求和、改述的列名）。

---

## 1. Coverage and method

- Analyses read: 50 of 50 (`q/UID*.json`). Every field aggregated with python; every `text_verdict`,
  `text_use`, `fix` and `notes` block read in full.
- Tally rule: one question counts once per label if any **text-carrying** cell (a, b2, c) received that
  label from the analyst; a question can therefore appear under two labels (kept in a, ignored in c).
  Cell b had no advice text and is excluded from the item tallies; it is used as the control in section 4.
- Smallest-change levels chosen by the analysts over the 50: **NONE 18, TOOL 17, TEXT 8, CONTROL 7**.
  Text is the first-choice lever on 8 questions: UID0027, 0096, 0101, 0102, 0109, 0117, 0149, 0237
  (UID0102 fixes no grade; every solver including the reference is wrong).
- First-divergence stages of wrong cells (from `first_divergence.stage`):
  - a (19 wrong): EXTERNAL_KNOWLEDGE 5, INTERPRETATION 4, SOURCE_ISSUE_VINTAGE 2, CELL_SELECTION 2,
    SOURCE_TABLE 2, OPERATIONAL 2, EXTRACTION 1, FORMULA 1.
  - b (30 wrong): INTERPRETATION 8, EXTERNAL_KNOWLEDGE 6, CELL_SELECTION 6, SOURCE_TABLE 4,
    SOURCE_ISSUE_VINTAGE 3, EXTRACTION 2, FORMULA 1, OPERATIONAL 1 (this one is the gate, UID0211).
  - c (26 wrong): SOURCE_TABLE 7, INTERPRETATION 7, EXTERNAL_KNOWLEDGE 5, CELL_SELECTION 2,
    ROUNDING_FORMAT 2, SOURCE_ISSUE_VINTAGE 2, EXTRACTION 1, FORMULA 1.
- How often the text was visibly used: in 49 of 50 a-cells, 24 of 25 b2-cells and 49 of 50 c-cells the
  analyst found at least one step quoting or acting on a sentence (`text_use` != none). The one exception
  in each is UID0030 (a chart-count question; nothing in the text applies). The text is read; the
  question is whether the sentence that matters is read at the moment it matters.
- Questions no text can move (analyst `irreducible` = yes, all solvers including the reference wrong):
  UID0030, 0037, 0114, 0183, 0188, 0214, 0230, 0240, 0102, 0155, 0165 (11 of the 18 a-and-b-both-wrong).
  The remaining 7 both-wrong questions are addressable: UID0062 (TOOL), 0215 (TOOL), 0180 (TOOL),
  0211 (CONTROL/TEXT), 0096, 0101, 0149 (TEXT).

---

## 2. Item-by-item verdicts for cell a's AGENTS.md

### 2.1 "Loops: a command repeated with the same output will not change. If a command already showed you its output, do something different ... or answer."
- kept-and-helped: **none** in 50 questions.
- ignored (13): UID0030 (a, ~137 three-command cycles, 447 requests, 56M input tokens, rescued only by a
  client compaction), UID0037 (a, ~135 repeats), UID0100 (a, 46 sed cycles), UID0114 (a ~30 cycles of a
  14-command block, b2 ~45 printings loops), UID0165 (a, 13-step block x41, 36M tokens), UID0183 (a, one
  rg 57 times), UID0188 (a 139 repeats; b2 12), UID0211 (a, 39 repeats), UID0213 (c, 3x rg), UID0214
  (b2, two 3x repeats), UID0215 (a, 8-step cycle x22; b2), UID0243 (a, six commands re-run), UID0245
  (a, 22 identical cycles). UID0029 and UID0225 are listed as "ignored" by the analysts but the looping
  cell was b (no text), so they are irrelevant for this tally.
- misled: none. irrelevant: 37.
- Cost evidence: cell a averages 66 requests and 6.45M input tokens per question against 23.5 / 0.76M
  for c with the same text and 34 / 2.55M for b; 86% of a's 322M input tokens come from its 8 looping
  questions. The mech arms' advisory loop note (threshold 3) ended b's loops on UID0030 (call 21),
  UID0037 (call 14) and UID0243, and only nudged on UID0114/0188/0214.
- **Verdict: DROP from text; move to CONTROL** (an enforced guard that refuses the 4th identical
  command, as proposed in UID0030, 0037, 0100, 0114, 0165, 0183, 0188, 0211, 0245). Zero evidence
  across 50 questions that the sentence is read at the moment it matters; UID0183's analyst: "repetition
  here is a degenerate model state ... which only a runtime refusal to re-execute can break."

### 2.2 "Compute every final number with `calc`, not in your head, and answer with the value it printed, rounded as the question asks."
- kept-and-helped (48): every question where a text cell computed something. Protective on UID0097 (c's
  calc caught a mental subtraction slip), UID0225 (Decimal re-do on a half boundary), UID0028 (a's
  panel-mapping error surfaced inside calc), UID0029 (the printed month count exposed a mapping bug in a
  and c), UID0100 (all index conventions printed in one run), UID0019 (b2[33] trusted calc over its own
  head arithmetic).
- partly ignored (1): UID0027 (a computed the final encoding in THINK, right anyway; c typed a two-digit
  year into calc, so calc did not help).
- misled (1): UID0102 ("answer with the value it printed": a copied float `round()` of an exact half;
  c overrode it with Decimal; both graded wrong, so no net effect, but the clause is literally what a
  followed).
- irrelevant (1): UID0030 (no arithmetic; all cells answered a bare integer).
- Contrast b (no text): mental sums on UID0117 (wrong, unprinted value) and UID0149 (wrong); mental
  mean on UID0224, mental count on UID0154, mental rounding on UID0225 (right by luck); no calc on
  UID0057, 0188; calc only after a gate round on UID0044, 0204, 0228.
- **Verdict: KEEP verbatim.** Optional guard for the UID0102/0225 half-boundary case belongs in the
  tool (a `round_half_up` note in calc), not in text.

### 2.3 "Provenance: use numbers you have seen printed by a tool. Before you rely on a value, print the line it comes from ..."
- kept-and-helped (36): UID0007, 0019, 0027, 0028, 0044, 0057, 0077, 0096, 0097, 0100, 0101, 0102,
  0109, 0112, 0114 (c, b), 0117 (**decisive and clean**: a and c printed each Total line; b, without
  the sentence, typed a hand-summed value no tool had printed), 0122 (a's raw read saw the group header;
  c's raw re-read fixed rounded inputs), 0130 (a printed the raw TSO-1 rows), 0133, 0135, 0148, 0154,
  0155, 0165 (kept but not protective: the tool's label was wrong), 0170, 0182, 0188, 0204, 0211, 0215,
  0220, 0225, 0228 (resolved a shifted header from raw lines), 0230, 0240, 0245.
- ignored (4): UID0037 (a[409] recalled values; c substituted narrative figures), UID0180 (a copied
  `%g`-rounded SUMMARY mantissas; the summary looked like a printed source), UID0183 and UID0214 (of
  necessity: the rate/CPI input exists in no tool output).
- misled, cost only (2): UID0213 (a[47], c[45] kept searching for a CPI line that does not exist),
  UID0243 (a spent ~25 steps on the same hunt, then used memory anyway).
- irrelevant: 8.
- **Verdict: KEEP**, with one clause for block tables (UID0117's proposal) and paired with the
  corpus-boundary sentence N1 so the sentence stops sending cells after series the corpus does not hold:
  > "Provenance: use numbers you have seen printed by a tool. Before you rely on a value, print the line
  > it comes from, so the number in your calculation is the number in the source. For a table with
  > calendar-year blocks, use the block's printed Total row; if the year stub looks wrong, anchor the block
  > by the dates in its Description cells."
  The UID0180 miss is a tool defect (printings SUMMARY rounds to 6 significant digits) and is fixed there.

### 2.4 "Answer count and format: give exactly as many values as the question asks for, in the order and the format it asks for ..."
- kept-and-helped (14): UID0007 (minor), 0028, 0037, 0057 (12-value list in order), 0096 (one value where
  the v8 none arm had hedged with two), 0097, 0100, 0101, 0133, 0148, 0170, 0218 (c checked the percent
  example), 0230, 0240.
- ignored (1): UID0114 (c answered a sentence; the grader counted three tokens, `count_mismatch(3 vs 1)`).
- misled: none. irrelevant: 35. Note that unit suffixes ('million INR', 'percentage points', 'inflow')
  were graded right on UID0228, 0077, 0044, so the format rule does not need to forbid them.
- **Verdict: KEEP**, rewritten to close the UID0114 hole:
  > "Answer count and format: give exactly as many values as the question asks for, in its order and
  > format (a bracketed list when it asks for one). The final line is a number or a list, never a
  > sentence; if a value cannot be found, still answer your best documented number."

### 2.5 "Percent or fraction: when the question asks for a percent, answer in percent (12.5, not 0.125); when it asks for a ratio, fraction or decimal, do not multiply by 100."
- kept-and-helped (11): UID0077, 0094, 0100, 0101, 0122, 0133, 0182, 0218, 0220, 0230, 0237 (a quoted it
  at a[22] to pick the unscaled ratio: decisive).
- ignored (3): UID0245 (c never invoked it although it decided the question, `scale_10^2`; a was right
  by an unexamined default), UID0237 (c had it and still weighted the units phrase more), UID0096 (a chose
  percent for a 'rate' with no percent cue; the bullet says nothing about 'rate').
- misled: none. irrelevant: 36.
- **Verdict: REWRITE** (merges UID0237, UID0245 and UID0096). The current bullet covers only the x100
  axis; b and c on UID0237 failed on the other axis (dropping the named operator), c on UID0245 let a
  unit phrase on the *inputs* set the output scale, and a on UID0096 had no rule for 'rate':
  > "Answer form: the metric the question names fixes what you report. A normalized difference, ratio,
  > share, rate or change is that metric computed from the cited values, never the raw difference of the
  > inputs. A unit phrase on the inputs ('yield (in percent)', 'in billions') does not change the output
  > form. Multiply by 100 only when the answer clause says percent, percentage or %; a rate or ratio with
  > no percent cue is a decimal."
  Risk: UID0237 is on the disputed list (issue47); if its key were changed to the dollar gap this rule
  would turn a wrong. Text is "evidenced weak" on UID0245 (c had the old bullet and never invoked it), so
  keep the submit-time `scale` check that flipped b wrong-to-right there as a backstop.

### 2.6 "Alternatives: when two candidate values fit (two tables, periods, printings or readings), weigh them against the question's exact wording and answer the one it asks for; do not average them."
- kept-and-helped (21): UID0019 (c chose the signed difference), 0028 (a rejected the interfund column),
  0057 (a: January-issue value over the revised March value), 0077, 0097 (b2), 0100, 0109 (a computed
  both DoD readings and chose), 0117 (c), 0122 (a), 0133 (a), 0148, 0155 (c weighed both printings),
  0204 (a: narrative vs FFO-1), 0211 (c: bonds vs notes), 0214 (chose by the word 'currency'; that is the
  reference's reading and is graded wrong), 0220 (a, formulas), 0225 (2-row reading), 0228 (b2), 0237
  (a), 0240 (no effect), 0245 (c chose midpoint over log).
- ignored (7): UID0028 (c: "full number = maximum precision" overrode the wording), 0096 (a voiced the
  narrower reading at a[34]/[45] and never computed it as a candidate), 0109 (b2 committed to the sum in
  one sentence), 0149 (a listed readings of 'TABs' and dismissed them after one empty grep), 0183 (a
  weighed rate precision, not the reading), 0188 (c picked a CPI base by guess), 0220 (b2 and c weighed
  the formula but never the table).
- misled (4): UID0062 (a[23] used the wording to pick the older vintage), 0211 (a weighed the
  parenthetical and picked the wrong same-title table), 0215 (all cells weighed only PDO-3 columns; the
  narrative candidate never entered the set), 0243 (a matched the word 'total' to the parent Total column).
  Followed-but-wrong at T=1 (4): UID0097 (c), 0122 (c), 0237 (c), 0214 (a, c).
- irrelevant: about 22 (including UID0130, where c never saw a second candidate, and UID0182, where c
  applied it between two wrong numerators).
- **Verdict: REWRITE.** The sentence is only as good as the candidate set the model has formed, and it says
  nothing about *how* to weigh. Keep the "do not average" core and add the two concrete tie-breaks the
  analyses found (candidate set includes other tables and narrative; more decimals is not a reason,
  UID0028):
  > "Alternatives: when two candidates fit (tables, sections, rows, printings, readings), name both,
  > compute both with calc, and answer the one the question's wording selects; never average. The
  > candidate set must include other tables and narrative sections, not only neighbouring columns. More
  > decimals or a later issue is not a reason to prefer a candidate."

### 2.7 "Bulletins revise earlier numbers. Unless the question names a publication date or issue, use the latest printing of each value: for every table cell you use, run `printings ...` and take the value printed by the latest issue listed."
- kept-and-helped (33): **decisive** on UID0019 (b2 would have been wrong without it, b2[23]), UID0100
  (four FY values revised, two vanish from later printings), UID0135 (the reference and v8_none took the
  single-issue vintage and are graded wrong), UID0180 (found the one revision), UID0224 (SUMMARY line
  decisive for every cell), UID0077 (surfaced two revisions), UID0133 (a); the pinned-source exception
  was applied correctly on UID0027, 0057, 0112, 0149 (c), 0154, 0165, 0204, 0225 (b2, c); harmless or
  costly-but-harmless on UID0007 (a; only because --years hid the later issues), 0028, 0044, 0094, 0096,
  0097, 0101, 0102, 0109, 0182, 0211 (b2, c), 0213, 0214, 0218, 0220, 0228, 0237, 0240, 0243, 0245.
- ignored (2): UID0062 (a ran printings, saw the revision, and overrode it because it read a
  column-description phrase as a date pin; c's printings hid the renamed series), UID0133 (c named it at
  c[21] and never ran it; used a preliminary snapshot).
- misled (11): UID0007 (b2: 'latest' overrode the question's own 1940-1949 scope, with the gate), UID0097
  (the `--row <period> --col <column>` idiom misled a twice on a dates-as-columns table), UID0114 (b2 ran
  printings for 48 cells, ~78 KB, in a loop), UID0130 (c: 13 identical printings gave false confidence in
  the wrong table; uninformative on snapshot tables), UID0149 (c[41] returned a different survey), UID0155
  (followed exactly; the key follows the first printing; issue47), UID0188 (b2 took the latest line of a
  mislabelled column), UID0211 (a took an isolated last-issue OCR variant as a revision; b did the same in
  its --alt), UID0215 (the latest PDO-3 printings were OCR-shifted; the rule pointed at corrupted cells
  and cost a most of 900 steps), UID0225 (10 failed calls on a release the as-of date pins), UID0245
  (listed a misparsed column from an older issue that sent a into a 22-cycle chase).
- irrelevant (9): UID0029, 0030, 0037, 0117, 0148, 0154, 0170, 0183, 0230 (snapshots never reprinted,
  releases pinned, or no table).
- **Verdict: REWRITE, in two bullets.** This is the single most valuable *tool-backed* item (the printings
  SUMMARY is what makes UID0100/0135/0180/0224 easy, and UID0019's analyst warns "do not soften it"), but
  the text overstates it ("for every table cell you use") and is silent on the pin cases and the OCR
  cases that the 11 misled questions share:
  > "Bulletins revise earlier numbers. Unless the question pins the source, use the latest printing of
  > each value: read the printings SUMMARY line for every value that enters your answer. A named issue,
  > publication or quotation date, 'as of' date, or table scope ('for the years 1940-1949') pins the
  > source: do not run printings on it; use each figure as that issue prints it, in the unit the question
  > quotes. A named issue pins only the quantity attached to it; a phrase describing what a column
  > includes pins nothing."
  >
  > "In printings, a value that only the last printing shows while earlier printings agree, or a value
  > whose column index differs from the other issues, is an OCR variant, not a revision. A table titled
  > 'Preliminary' or a one-date snapshot is a first printing: find the revised value in the section's
  > time-series table."
  Companion tool fixes the analyses insist on (not text): SUMMARY printed verbatim (UID0180), grouped by
  section/panel/as-of date (UID0007, 0044, 0170, 0211), series followed across a title change with the
  revision note printed (UID0062), snapshot-aware mode (UID0130, 0117), end-of-series message (UID0114),
  column-index warning (UID0188, 0228).

### 2.8 "Match the period exactly. FY (federal fiscal year: July-June through FY1976, October-September from FY1977) and CY rows are different rows even with the same year number ..."
- kept-and-helped (18): UID0027 (a filtered 1960-1969), 0028 (caught a mislabelled printing), 0029
  (both text cells checked for 120 months), 0077 (**decisive**: CY built from 12 monthly rows; the v8 none
  and cards arms failed exactly here), 0094, 0096 (a and c never took the calendar-year issues; b lost ~25
  steps to them), 0101, 0102 (a and c took 12 Oct-Sep rows; b took 13), 0109 (excluded '- Est' and 'to
  date' rows), 0112, 0133 (a matched the 'Calendar Year 2002' header), 0135, 0182, 0211, 0213 (c chose
  December rows), 0228, 0240, 0243.
- ignored (1): UID0037 (c used annual averages for Q2 bars). Partial: UID0027 (c compared only 7 months).
- misled: none. irrelevant: 31.
- **Verdict: KEEP**, shortened (the FY-definition clause was never cited) and with UID0102's month-count
  clause:
  > "Match the period exactly: FY and CY rows with the same year number are different rows; '- Est' and
  > 'to date' rows are not the year's row; a fiscal year has exactly 12 month rows (FFO tables print a
  > 13-month window: drop the first month). Check the filled period of every cell you use."

### 2.9 "show_table marks rows it repaired (SHIFT+1, SPLIT, ROWSPLIT), month rows whose year is not printed (Y?), and rows where the table's total identity FAILS. If a value you use sits on such a row, compare it with another printing ..."
- kept-and-helped (8): UID0007 (a, decisive but ~25 steps by hand; a[45] notes the sentence "doesn't
  explicitly say choose majority"), 0027 (a saw the Y?/SIBLINGS note and opened the sibling), 0028
  (weakly), 0096 (a[53] compared a corrupted total with other printings and a monthly sum), 0117 (partly:
  a went to --raw after the repair note), 0213 (a, c compared printings), 0215 (c compared and rejected
  shifted rows that show_table had NOT marked), 0228 (b2 checked an identity-FAILS cell).
- ignored (3): UID0007 (c and b2 had the FAILS note on screen and skipped it), 0027 (c's `--from-row 1`
  hid the NOTES block), 0057 (a took the first value of a ROWSPLIT merged row without comparing; harmless).
  Could not fire (1): UID0019 (the shifted rows carried no mark; b found the shift by hand).
- misled: none. irrelevant (38): including five questions where the defect was a **column-label** shift
  or a lost header group that this row-only sentence does not cover: UID0165 (a, b, b2 all read past the
  tool's own 'spurious extra label' note), 0148, 0213, 0214, 0149.
- **Verdict: REWRITE** to say what to conclude after comparing and to cover column warnings; keep it short
  because the analysts (UID0007, 0165, 0148, 0214, 0029) want the tool to print the repaired value or
  anchored year instead:
  > "Read show_table's NOTES block on every page (--from-row 0, or --raw plus NOTES). A row flagged
  > 'identity FAILS', 'Y?' or repaired: compare with other printings and take the value most printings
  > agree on that restores the row total; for Y? rows open the SIBLINGS printing to read the years. A
  > header flagged 'spurious extra label' or 'shifted', or a column listed under different indexes across
  > printings: map the numbered labels (1)..(n) onto the n numeric columns in order and confirm with the
  > identity line before reading a column by its label."

### 2.10 "Several tables in one issue can look alike. Search with find_tables using the question's key nouns and prefer the table whose title and column headers match the question's wording; note its id so printings can follow it across issues."
- kept-and-helped (30): UID0007 (minor), 0019 (all cells followed the table by id), 0027, 0028 (a:
  'Trust Receipts' table), 0044, 0057, 0062, 0077, 0100, 0101 (cost b2 six steps verifying FFO-3 = FFO-4),
  0109 (c read 'on-budget and off-budget' as a table descriptor), 0122, 0130 (a, b2 chose TSO-1 by title),
  0133 (a), 0135 (with an 8-step detour), 0148, 0154, 0155, 0165, 0170 (b2 rejected the general survey),
  0180 (a, b2, c picked OFS-1 over FD-1 by header wording; b without the text took the only table it saw),
  0183, 0213, 0214, 0220 (a, **decisive** re-search for the broader total), 0224, 0225, 0228, 0237, 0245.
- ignored (9): UID0028 (c chose the more-decimals detail table), 0029 (phrasing failed; id search worked),
  0094 (harmless: the tables carry no id), 0097 (find_tables found nothing; a/b2 fell back to rg), 0117
  (no 1970s PDO-1 in the index; rg fallback), 0130 (c accepted OFS-1 without comparing), 0218 (paraphrase
  shares no words with the corpus), 0220 (b2 and c took the component table's Total), 0243 (a's cut
  listing showed one group; c abandoned find_tables).
- misled (7): UID0102 (a and c spent most of their steps hunting a table titled with the question's
  words and rejected FFO-2, whose column is 'Corporation > Net'), 0117, 0133 (c: title-word match ranked a
  preliminary country snapshot first), 0149 (weakly: Table 2's title matches, the rows are in Table 3),
  0182 (b2, c: the literal matches were the wrong tables; the right table matches only in its row labels),
  0211 (a: no tie-break for two same-title tables; 8 steps guessing ids that 1950s tables lack), 0215 (all
  four cells obeyed it and got PDO-3; the answer lives in a narrative section find_tables does not index).
- irrelevant: 4 (UID0204 prose sources, 0030, 0037, 0230).
- **Verdict: REWRITE.** "Title and column headers match the question's wording" is right as a
  summary-vs-component tie-break (UID0220) and wrong whenever the literal match is a decoy (UID0102,
  0133, 0182, 0215). Most of the cure is in find_tables itself (row-label search, section index,
  group-first listing: UID0182, 0215, 0243, 0133, 0130); the text should say what to do when the listing
  is wrong or empty, and give the two tie-breaks the analyses established (UID0211, 0135, 0220, 0215):
  > "Several tables look alike. Search with the question's key nouns; if 0 groups or no good match, retry
  > with fewer words and with --rows (row labels). Prefer the table whose ROW or column label is the
  > question's noun phrase, then the title. A title '<X> - General' is a component: for 'total <X>' find
  > the summary table with '<X> > Total'. Two tables with one title: tell them apart by show_table's
  > section line; a parenthetical such as '(U.S. Treasury)' names the publisher, not a section, so take
  > the first table unless the question uses the section's distinguishing word ('notes', 'bonds'). A
  > question naming a report or section may mean narrative text: rg the phrase."

### 2.11 "For a sum over the columns of one row, list the whole row (show_table <file> <line> --long --grep <period>) and decide for each column whether the question includes it ..."
- kept (1): UID0149 (a[15] listed the row; not decisive).
- misled (1): UID0109 (b2: the sentence presupposes a sum; it read 'total ... Department of Defense' as
  military + 'Other Defense, civil programs' and never listed the single-column variant; b did the same
  without text; all five text-only runs across v8 and v9 chose the single column).
- irrelevant: 48 (including UID0044 and UID0122, whose sums run down a column or across rows, shapes the
  sentence does not cover).
- **Verdict: REPLACE** with the entity rule (N3 below), which is what UID0109, 0243, 0097 and 0096 ask
  for in four different phrasings.

---

## 3. NEW text items the analyses ask for (ranked by question count)

Each item: exact sentence (<= 60 words), the questions it would fix or shorten, and the risk to right cells.
"fix" = the analyst's `would_have_fixed`; "cost" = steps saved without changing the grade. Items marked
(folded) are already part of a section-2 rewrite and are listed here because several analyses asked for
them separately.

**N1 (11 questions; cost only). Corpus boundary: outside series and chart values do not exist.**
> "The corpus holds Treasury Bulletins only: no CPI, GDP, exchange-rate or population series, and charts
> survive as titles and axis ticks without plotted values. If a question needs such a value, one
> find_tables and one rg confirm it is absent: stop searching, decide whether it cancels, otherwise name
> your best recalled value in calc and answer." (58 words)
- Questions: UID0114 (a 30 cycles, b2 45 cycles hunting a discontinued value; c answered a sentence),
  UID0122 (all four cells hunted for CPI; it cancels), UID0165 (FX rate absent; 15-25 steps per cell),
  UID0183 (all cells kept searching after concluding there was no table), UID0188 (silver price absent),
  UID0213 (25-35 steps per cell on CPI), UID0214 (20-40 steps per cell), UID0228 (15-35 steps per cell on
  a rupee rate), UID0243 (a ~25 futile steps), UID0030 and UID0037 (chart values; every cell spent 15-60
  steps looking for images, PDFs, sqlite blobs).
- Correctness: none. The grade on UID0214/0243/0183 still depends on recalled values, which vary by fork;
  the analysts prefer a read-only reference-series tool for that (TOOLS.md). On UID0183 the analyst
  adds "derive, do not recall": c computed a day-weighted blend and discarded it for a recalled figure.
- Risk: none to right cells. Benchmark-specific; belongs in the OfficeQA notes, not the domain-general part.

**N2 (8 questions; cost, plus one guard). Pinned source: issue, as-of date, scope.** (folded into 2.7)
> "A named issue, publication or quotation date, 'as of' date, or table scope ('for the years
> 1940-1949') pins the source: do not run printings on it; use each figure as that issue prints it, in
> the unit the question quotes. A named issue pins only the quantity attached to it; a phrase describing
> what a column includes pins nothing." (58 words)
- Questions: UID0007 (b2's 'latest' overrode the scope; fixes b2 only if the gate stops contradicting
  it), UID0028 (a, b, c each burned 20-50 steps in the pinned issue looking for the second hop), UID0057
  (a's exception-clause override is the behaviour to protect), UID0062 (a read a column-description phrase
  as a date pin; the analyst rates text alone insufficient because b and c never saw the later printing),
  UID0148 (five dead-end calls; and the reference lost the question by substituting a cross-vintage value
  for a pinned printed cell), UID0170 (every cell first opened the December issue before accepting the
  survey's as-of date), UID0204 (the reference and v8_none 'upgraded' a pinned prose figure to a table
  cell and were wrong: "more diligence hurts"), UID0225 (10 failed printings calls).
- Risk: none observed; a and c already behave this way when they read the exception.

**N3 (5 questions; correctness). Entity = its own column; qualifiers narrower than a total.** (replaces 2.11)
> "An agency, program or item the question names is the ONE column (or row) whose label names it: not
> the parent 'Total' beside it, not a neighbouring 'Other' or 'civil' column, unless the question says
> 'including' or lists the parts. Question words repeating the table title or units line ('on-budget and
> off-budget', 'nominal') describe the table, not extra columns." (59 words)
>
> "A qualifier narrower than a total row ('goods subject to duty' vs 'total dutiable and taxable') means
> the footnoted component the qualifier excludes ('Free list', 'nonmarketable') must be subtracted from
> every operand; compute both readings with calc and answer the one that honours every word." (44 words)
- Questions: UID0109 (fixes b, b2), UID0096 (fixes a, b, c: every cell had the footnote on screen and
  still took the total row), UID0243 (a: 'total' -> parent Total column), UID0097 (c: 'nominal' read as
  excluding a row; T=1 flip, low confidence), UID0220 (b2, c took the component table's Total; folded
  into 2.10).
- Risk: unlikely; a and c already reasoned this way on UID0109; the 'including/combined' escape keeps
  genuine multi-part totals summable; the second sentence fires only when a footnoted component sits inside
  a total the question narrows. UID0096's analyst rates it medium; UID0097's modifier clause low.

**N4 (3 questions; correctness). Answer form is fixed by the named metric.** (the 2.5 rewrite)
- Questions: UID0237 (fixes b, c; issue47), UID0245 (c, `scale_10^2`; text "evidenced weak"), UID0096
  (a's percent-for-'rate' choice).
- Risk: a question wanting a raw dollar difference names no normalizing metric, so the sentence stays
  silent; if the UID0237 key were changed to the dollar gap the rule would turn a wrong.

**N5 (3 questions; correctness 2, cost 1). printings disagreements that are OCR, not revisions.** (folded into 2.7)
- Questions: UID0211 (a promoted an isolated last-issue variant; b did the same in its --alt), UID0133
  (c treated a preliminary snapshot as final and skipped printings), UID0228 (a, b, c each spent 3-6 steps
  discovering that a column listed under different c-indexes was a shifted header; the 'latest printing'
  SUMMARY line stated a wrong-column value).
- Risk: low; a genuine late revision usually appears in more than one later issue. The analysts prefer a
  printings SUMMARY line ('latest printing differs from all N earlier, which agree') and a column-index
  warning over text.

**N6 (1 question, fixes 3 cells; correctness). Parenthetical method is the definition.**
> "Definitions: when the question names a measure with a parenthetical method ('arc elasticity (using
> midpoint percentage change)'), compute exactly what the parenthetical describes from the series given.
> Do not add a variable the question does not name (the year number, time). One value hundreds of times
> larger than its siblings means you did." (52 words)
- Question: UID0101 (a, b, b2 divided by the midpoint change of the year number; c did not). Only lever
  on this question; the reference made the same textbook choice and is graded wrong. Related but NOT
  recommended: UID0240 ("write the formula out with the question's terms": the reference did exactly this
  and was wrong; the key is within 0.1% of the trivial reading but not string-equal) and UID0230
  (annualization factor: a guess at the grader).
- Risk: unlikely; fires only with a parenthetical and one series.

**N7 (2 questions, 3 related; correctness 1). Header label shift and Y? rows.** (folded into 2.9)
- Questions: UID0165 (a, b, b2 read past the tool's own 'spurious extra label' note; the analyst warns "a
  sentence competes with the same inattention"), UID0029 (the sibling-anchoring clause; cost only; the v8
  cards arm applied it with the wrong sibling and failed). Related tool defects on UID0148, 0213, 0214.

**N8 (2 questions; correctness 1, cost 1). Same-title tables and section phrases.** (folded into 2.10)
- Questions: UID0211 (a chose the notes table on the parenthetical; the reference did the same and is
  graded wrong; medium-low confidence), UID0135 (a's 8-step detour to a table whose column merely
  contained 'reported by banks', which names the section).
- Risk: the 'first table' default is a benchmark convention; keep it in the evolvable part.

**N9 (1 question; fixes c). Encoded answers use full values.**
> "Encoded answers: when the question defines the answer by a formula ('multiply the month by 100 and
> add the calendar year'), evaluate it in calc with full values: a calendar year is four digits, a month
> 1-12. Check the digit count the formula implies before answering." (45 words)
- Question: UID0027 (c typed a two-digit year into calc; the encoding was mis-evaluated in 5 of 6 wrong
  runs across v8 and v9). Risk: none (a already did this).

**N10 (1 question; fixes a, b; officeqa_specific). Acronyms are literal; grep the expansion.**
> "Security acronyms in a question are literal names, not typos: TABs = tax anticipation bills, a
> sub-block under Treasury bills in the Survey of Ownership by-issues table (Table 3). Before calling a
> term a typo, rg its expansion and the bare acronym across the whole corpus, and open the by-issues
> table, not only the summary." (56 words)
- Question: UID0149 (a and b read 'TABs' as a typo for Treasury bills; c expanded it). Risk: none; the
  glossary clause is benchmark-specific and belongs in the evolvable text rather than the seed.

**N11 (1 question; fixes a, b, b2, c only together with a section index in find_tables). Narrative sections.**
> "A question naming a report or section ('Treasury Financing Operations', 'Auction of ...') may point
> to narrative text, not a table: rg -n -i the phrase and read that section in the issue covering the
> period. In auction paragraphs 'tenders accepted' is the 'of which $X was accepted' figure; 'In addition
> ... Federal Reserve' amounts are excluded." (56 words)
- Question: UID0215 (all cells took PDO-3; b and b2 grepped the narrative and lost it to head/-m
  truncation). Risk: none to right cells; the analyst rates text alone insufficient.

**N12 (1 question; fixes b's primary and c). Group header membership.**
> "A group header with no subtotal row (a label ending in a colon, no values) covers only the rows of
> its kind that follow it; a receivable, payable or other item of another kind listed after them is
> separate unless a printed subtotal includes it. Name the rows you include and check they foot to the
> section total." (58 words)
- Question: UID0122. Risk: the reference itself calls the receivable question a residual ambiguity, so a
  hard rule may be wrong on another table; better after show_table prints the header and the footing.

**N13 (1 question; guards c-like slips). Date-window rows.**
> "When a question limits rows by a date window (issued, matured or dated between X and Y), print the
> (date, value) pairs from that date column, keep only rows inside the window, and check that the number
> of kept rows equals the count you use before computing." (47 words)
- Question: UID0148 (c hand-typed a January row and was saved by a length check; b's error was a
  column-semantics misread the tool must fix). Risk: none.

**N14 (1 question; cost). Ratios from one table in one unit.**
> "A ratio's numerator and denominator should come from one table in one unit. If your candidates are in
> different units or reporting populations, search again with --rows for a table holding both rows
> instead of converting between them." (39 words)
- Question: UID0182 (b2 noticed CAD vs USD and converted instead of re-searching). Risk: none; the
  analyst says text cannot change what find_tables returns, so the tokenizer/row-label fix comes first.

**N15 (1 question; fixes b). Block tables use the printed Total row.** (folded into 2.3)
- Question: UID0117. Risk: none (a and c did this).

**N16 (1 question; coin-flip insurance). Regression on an undefined index.**
> "When a question says to regress on a 'year index' or 'time index' without defining it, print the
> 1-based, 0-based and calendar-year fits and answer the 1-based one unless the question defines the
> index." (37 words)
- Question: UID0100 (all cells right; v8 lost it to a 0-based awk). Risk: a future 0-based key.

**N17 (1 question; identity check only). Stacked panels.**
> "A table may be printed as stacked panels (component blocks, then a NET or TOTAL block). If the total
> block lacks the row you need, sum that row across the component panels with calc and, where another
> issue prints the table completely, check on one row that total = sum of components." (52 words)
- Question: UID0044 (all cells did the first half unprompted). Risk: none.

**N18 (1 question; changes b's divergence, no grade). FY has 12 month rows.** (folded into 2.8)
- Question: UID0102 (b took 13 months; a and c 12; all graded wrong for a convention reason).

**N19 (1 question; fixes c). More decimals is not a reason.** (folded into 2.6)
- Question: UID0028 (c[100] "full number = maximum precision" overrode the wording).

**N20 (1 question; cost). Paraphrased column names.**
> "When the question paraphrases a column rather than quoting its header, list the candidate table's
> column headers (show_table --cols) and pick the header whose meaning matches; do not stop at the first
> label that shares a word." (38 words)
- Question: UID0218 (all cells right; found the table by browsing the section). Risk: none.

**Not recommended as text** (low confidence, unverifiable, or a tool defect):
UID0220's percent-difference convention (the reference's opposite convention is graded wrong here, but
the cross-question convention is unchecked); UID0214's "inflation-adjusted means deflate" (the key's
reading cannot be established; could flip other questions); UID0230's annualization factor and
UID0240's "write the formula out" (the reference did and was wrong); UID0102's "compute every rounding
reading and pass --alt" (records alternatives, identifies nothing); UID0180's "printings SUMMARY rounds
to 6 digits" (fix the tool); UID0097/0122's "row = label, col = date on dates-as-columns tables" (fix
printings' error message); UID0130's one-unit tie-break (c never saw the alternative; a benchmark
convention with "no decisive evidence either way").

---

## 4. What the text is worth: b (no advice) vs a (advice)

**Raw scores (50 questions).** a 31, b 20, c 24; reference solver 29 (a beats the reference on UID0019,
0130, 0135, 0148, 0182, 0204, 0220; loses on UID0096, 0149, 0180, 0215, 0243). Pairwise a vs b: both
right 19, both wrong 18, **a-only 12**, **b-only 1** (UID0243, where b's recalled CPI values happened to be
right and a's text-guided 'total' reading was wrong). On the first 25: a 15, b 9, b2 13, c 12.

**Confound.** b differs from a in two things at once: no advice text AND a submit gate (10 rules with
Accept/Keep offers, loop notes, compaction splice). The 12 a-over-b wins decompose, per the analyses:
- Text credited (5): UID0007 (a acted on the FAILS-row sentence; but c and b2 had it and skipped it),
  UID0109 (a's Alternatives weighing; b2 with the same text failed), UID0117 (**clean**: both text cells
  printed each Total line, b hand-summed an unprinted value), UID0220 (a's re-search for a broader total;
  b2 and c with the same text did not), UID0237 (a quoted the Percent bullet; c with it failed).
  Partial: UID0122 (a's provenance raw-read exposed the group header; b2 and c with the same text failed).
- Gate hurt b (1): UID0097 (competing_candidates echoed b's own --alt as an Offer; b's first reading was
  right). The same rule flipped b's right answer on UID0211 (a 0, b 0).
- Tool / search luck, text irrelevant to b's error (6): UID0027 (b mis-decoded its own argmax index),
  0028 (panel-year mapping), 0133 (untitled reprint invisible to printings), 0148 (column-semantics
  misread), 0182 (search luck: a's fifth phrasing hit), 0122 (see partial above).
- Text cost a (1): UID0243 (Alternatives + 'prefer matching wording' mapped 'total' to the parent Total).

So the text's gross value is about **+5/50** and its *robust* value (fires in every text cell) is about
**+1 to +3**: on four of the five text-credited questions another cell with identical text failed, and the
text lost one question outright.

**Cleaner controls.**
- b -> b2 (same mechanism family, add a's text, drop 5 rules): **+4 / -0** on 25 (UID0180, 0097, 0155,
  0211). UID0097 and 0211 are documented gate-offer removals (CONTROL); UID0155 is luck (truncated tool
  output pushed b2 onto the first printing, which the disputed key follows); UID0180 is
  text-attributable (b2 chose OFS-1 by header wording; b saw only FD-1). Text share: **+1**.
- a vs b2 (identical text, different mechanism): 15 vs 13 on 25; a wins 5 (UID0122, 0220, 0007, 0109,
  0182), b2 wins 3 (UID0180, 0155, 0211). The same text produces +-3-5 question swings between cells,
  i.e. the text effect is smaller than run-to-run variance (consistent with the earlier 26/19/22
  baseline spread).
- c vs b (text at T=1 vs no text at T=0 with gate): 24 vs 20; c-only 7 (UID0180, 0101, 0109, 0211,
  0117, 0149, 0148), b-only 3 (UID0130, 0245, 0243). Two of b's three wins are gate offers (UID0245
  `scale`) or recall luck (UID0243).
- c vs b2 (same text, T=1 vs T=0 with rules): 12 vs 13 on 25; the text at T=1 is worth nothing over the
  text at T=0 with a small gate.

**Where the text clearly works.** Three sentences carry it: calc (48 kept, b's mental arithmetic lost
UID0117 and 0149), provenance (36 kept; decisive on UID0117), and the latest-printing rule (decisive on
UID0019, 0100, 0135, 0180, 0224; b reached the same choice unaided on UID0019, 0100, 0135 via the README
convention, so even here the text is a reminder, not the only source).

**Where the text clearly does NOT work.** Loops (0/13), and any sentence that asks the model to notice
something a tool printed wrongly or hid: UID0165 (label shift), UID0180 (rounded summary), UID0148 (lost
header group), UID0122 (hidden group header), UID0215 (unindexed section), UID0133 (untitled reprint),
UID0062 (renamed series), UID0102 (question-worded capsule tables un-indexed). In all of these a cell
carrying the relevant sentence failed anyway, and the analysts chose TOOL as the smallest change (17/50).

**Where the text is a liability.** The latest-printing rule misled 11 questions (cost on most, grade on
UID0211 and, against a disputed key, UID0155); the 'prefer matching title/headers' rule misled 7; the
Alternatives rule 4. Each is a good rule with a missing exception, which the section-2 rewrites supply.

**Cost.** The text arm has no loop guard; a's mean cost (66 requests, 6.45M input tokens/question) is ~2x
b's and ~8x c's, entirely from 8 looping questions (86% of a's tokens). That cost belongs to the missing
control, not to the text, but any v10 text-only comparison must add the guard or be dominated by it.

---

## 5. Recommended v10 seed text (assembled from the verdicts)

Working advice (domain-general): 2.2 calc (verbatim), 2.3 provenance (+ block-table clause), 2.4 answer
format (rewritten), 2.5 answer form (rewritten), 2.6 alternatives (rewritten), N6 definitions, N9 encoded
answers, N13 date windows, N16 regression index (optional).

OfficeQA table notes (document-QA / benchmark-specific; the part the learner should evolve first): 2.7
latest printing (rewritten, two bullets, includes N2 and N5), 2.8 period (shortened, includes N18), 2.9
NOTES block (rewritten, includes N7), 2.10 finding tables (rewritten, includes N8 and N11), N3 entity
rule (replaces 2.11; includes N19's precision clause via 2.6), N1 corpus boundary, N10 glossary, N12
group headers, N14 one-table ratios, N17 stacked panels, N20 paraphrased columns.

Dropped: Loops (to CONTROL). Also drop from any submit note the sentence that the harness "computed that
alternative itself" when the offer is the model's own --alt (UID0097, 0122, 0211, 0028); b2 still carries
it and it did no harm only because no offering rule fired.

Do not add: a "prefer the more precise printing" clause (UID0019), a "find the exact table value" push
(UID0204), a "repair suspected OCR values from other issues" rule (UID0148: the reference lost the
question that way; GLM's literal-copy default was right), a first-printing preference (UID0155).

Expected effect if only the text changes: the TEXT-first questions UID0027 (c), 0096 (a, b, c), 0101 (a,
b, b2), 0109 (b, b2), 0117 (b), 0149 (a, b), 0237 (b, c) plus partial credit on UID0211/0243/0062 (a),
i.e. for cell a roughly **+3 to +5 on this 50** if every sentence is read and about half that given the
observed read-rate; the 17 TOOL-first questions and the 11 irreducible ones stay where they are. The
learner should score the text only after the loop guard, the printings SUMMARY fixes and the find_tables
row-label/section index are in place, or the text signal will be buried under tool noise.
