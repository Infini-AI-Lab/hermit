# v10 synthesis — CONTROLS (loop guard, compaction, submit gate, output caps, temperature) and everything else

Scope of the evidence: all **50** per-question analyses (`q/*.json`, one per task id, count verified = 50) plus `index.json` (correctness, steps, tokens, compactions, gate rounds, error bins). This version replaces the earlier partial synthesis that covered 26 questions; every count below was recomputed over the 50 files. No answer key, grader or evidence file was opened, and no answer value is written here. Cell steps are cited as `a[12]`, `b2[7]`; questions by task id.

---

## 中文摘要（写给初学者）

先解释几个词（第一次出现时都解释一下）：

- **harness adapter（harness 适配器）**：模型做题时"外面套的那层壳"。它能决定三样东西：给模型看的文字（AGENTS.md）、工具（这里是四个表格工具 `find_tables` / `show_table` / `printings` / `calc`）和 **controls（运行时控制）**。本文只管第三样。controls 就是"不是靠模型自觉、而是由程序在运行时强制或提醒"的规则，比如"同一条命令重复三次就插一句提醒"，或"提交答案时自动检查格式"。
- **cell（格子）**：一种配置在 50 道题上跑一遍。四个格子：**a**（只有文字，温度 0，31/50 对）、**b**（没有文字，靠 10 条提交时规则，20/50）、**b2**（a 的文字 + 5 条规则，只跑了 25 题，13/25；a 在同 25 题上 15/25）、**c**（a 的文字，温度 1.0，24/50）。
- **temperature（温度）**：采样随机度。0 = 每次几乎走同一条路；1.0 = 会随机换路。
- **submit gate（提交闸门）**：b/b2 里模型不是直接说答案，而是运行 `submit`；harness 用一组规则检查，返回 **finding（发现）**，有时附带 **offer（替换建议）**，模型可以 Accept（接受）或 Keep（保留原答案）。
- **loop guard（循环守卫）**：b/b2 里，同一条命令原样重复 3 次，harness 插一句"你已经跑了 3 次一样的命令"，最多插 3 次。a/c 没有这个，只在文字里写了一句"重复的命令结果不会变"。
- **runaway（跑飞）**：一道题跑了超过 150 步（正常题 15-60 步）。
- **context compaction（上下文压缩）**：对话太长时把前面内容压成摘要。b/b2 压缩后 harness 会把 **ledger（账本，工具输出的权威记录）** 重新贴回去。
- **issue-47**：官方题目或答案被公开质疑的题；**irreducible（不可约）**：无论怎么改适配器都救不回来的题（题目歧义、答案键约定不明、需要语料里没有的外部数据）。

结论（每个数字都能在下文英文部分核对）：

1. **提交闸门的"offer + Accept"机制要删掉。** b 的 27 道题有闸门回合，真正改了答案的 7 次：1 次错→对（`UID0245`，scale 规则），**2 次对→错**（`UID0097`、`UID0211`：competing_candidates 把模型自己的 `--alt` 回声成"offer"，模型照单全收），4 次错→错（`UID0122`、`UID0007`、`UID0165`、`UID0028`）。净效果 **-1 题**。b2 去掉了 competing_candidates，这两题都对了；b2 自己的 2 次闸门改动（`UID0007`、`UID0114`）也全是错→错。75 次机制格子运行里，闸门只救了 1 题。
2. **十条规则里能留的只有 `scale`（修一下）和 `recompute_final`（便宜、0 次伤害、抓过一次 shell 把提交串弄坏的情况）。** `competing_candidates`、`table_alternatives`（4 次全误报）、`row_coverage`（3 次全误报）、`period_match`（0 次触发）删掉；`later_printing` 2 次触发都指到了错误面板的行，而且在 3 道本该触发的题上没触发（`UID0155` 若触发会把唯一对的 b2 变错；`UID0133`、`UID0062` 若触发会帮忙），在 `printings` 工具按（部件、面板、期间、列）正确定位单元格之前不要开；`grounded` 10 次触发 0 次改答案、约 6 次误报，改成无 offer 的提示；`flagged_cells` 6 次触发 3 次绑错了表；`answer_count` 留着但不能"裸拒绝"（`UID0114` b2 一次诚实的"算不出来"被打回去，两步后编了个数）。
3. **循环守卫要从"提醒"改成"强制"，而且文字组也要有。** a（无守卫）9/50 题跑飞，吃掉 a 全部 3.22 亿输入 token 的 **90%**；b 收到 21 条提醒仍有 4 题跑飞（`UID0029` b 被点名的命令又重复了约 136 次；`UID0188` b 约 294 次；`UID0114` b 约 85 次）；c（温度 1）0 题跑飞。AGENTS.md 里那句"Loops: …"在 50 份分析里被评为 **ignored 15 次、irrelevant 34 次、kept-and-helped 0 次**。
4. **压缩本身没有改对/改错任何答案**，但只在跑飞的题里出现（b 4 次、b2 1 次、a 约 8 次 Codex 自带压缩），而且账本回贴的内容两次是噪音（`UID0188` b 贴的是不相干的进口配额表；`UID0183` b 贴的 1940 年代表格把模型带去查不相干的 India 行）。留着回贴，但按题目名词或表格形状挑内容，不要"最近碰过的"。真正要做的是让跑飞不发生。
5. **温度 1 不是免费的多样性**：c 比 a 少 7 题（a 对 c 错 11，c 对 a 错 4），输的多是一步之差的判断。评分用 T=0，用强制循环守卫代替"靠随机跳出循环"。
6. **13 道四格全错的题**：10 道不可约（图表、外部 CPI/汇率/银价、答案键的取整或年化约定谁都猜不中：`UID0030`、`UID0214`、`UID0188`、`UID0165`、`UID0114`、`UID0183`、`UID0102`、`UID0230`、`UID0240`、`UID0037`），3 道可修（`UID0215` 叙述段落索引、`UID0096` 脚注限定词句子、`UID0062` printings 跨改名跟踪 + 打印修订注）。
7. **新发现的两个控制点**：(a) 工具命令 exit 0 但输出 0 字节、harness 没抓到（`UID0154` b2[13]、`UID0170` c[14]，离线重跑都正常）：要有"空输出重跑一次"守卫；(b) 5 个机制格子把 `submit` 当成答案键探针（`UID0030` b2、`UID0037` b、`UID0240` b、`UID0148` b、`UID0122` b），说明"闸门静默 = 通过"会被误读，v10 的 submit 要说明它不是判分器。

---

## 1. What the four cells' controls were

| control | a (text-static, T=0) | b (mech-static, pilot 1) | b2 (mech-static, pilot 2) | c (text-static, T=1) |
|---|---|---|---|---|
| advice text (AGENTS.md) | ~2.5 KB | none (829-byte submit note) | a's text + submit section | a's text |
| loop guard | none (one sentence in text) | advisory note after 3 identical commands, `loop_notes_max` 3 | same | none |
| compaction handling | Codex native | Codex native + ledger splice | same | Codex native |
| submit gate | none | 10 rules; findings with Offer/Accept/Keep; `gate_findings_per_round` 1; `gate_rounds_max` 2; `--alt` solicited | 5 rules (answer_count, later_printing, period_match, recompute_final, scale); no `--alt` solicitation | none |
| output cap | 8000 bytes per command | same | same | same |
| sampling | T=0 | T=0 | T=0 | T=1.0 / top_p 0.95 |
| budget | 3600 s | same | same | same |

Score: a 31/50, b 20/50, c 24/50; on the first 25: a 15, b 9, b2 13, c 12.

Cost (index.json): a 7.3 h / 322 M input tokens / 3306 requests / 6620 steps; b 4.1 h / 127 M / 1719 / 3578; c 2.3 h / 37 M / 1177 / 2429; b2 (25 q) 1.3 h / 20 M / 545 / 1165.

Where the 50 analyses put the smallest fix: **TOOL 17, NONE 18, TEXT 8, CONTROL 7**. Of the 7 CONTROL-first fixes, 4 are loop guards on irreducible questions (`UID0030`, `UID0188`, `UID0183`, `UID0037`: cost, not correctness), 2 are removals of the own-`--alt` offer (`UID0097`, `UID0211`), 1 is a repair/extension of `scale` (`UID0245`). No analysis proposes a *new* offering rule as its first fix; 46 CONTROL ideas appear as alternatives, almost all with "why_not_first: it can only nag / it needs a ledger binding the pilot did not have / it fires after the work is done".

---

## 2. Loops and loop notes

### 2.1 Runaway runs (>150 steps)

| cell | runs >150 steps (task: right?, steps, M input tokens) | share of the cell's input tokens spent in them |
|---|---|---|
| a | 9: `UID0215` (0, 901, 55), `UID0100` (1, 208, 12), `UID0030` (0, 896, 56), `UID0188` (0, 352, 24), `UID0165` (0, 573, 36), `UID0114` (0, 849, 53, timeout, empty answer), `UID0183` (0, 306, 16), `UID0245` (1, 268, 16), `UID0037` (0, 415, 22) | **90 %** |
| b | 4: `UID0188` (0, 677, 48), `UID0114` (0, 298, 17), `UID0183` (0, 270, 11), `UID0029` (1, 394, 23) | 78 % |
| b2 | 1: `UID0114` (0, 270, 9) | 45 % |
| c | 0 (max 123 steps, `UID0188`) | 0 % |

Shapes, as the analyses describe them: a fixed cycle of 3-14 commands repeated 20-140 times with byte-identical output until Codex compaction resets the thread. `UID0030` a[26]-[846]: a 3-command cycle ~137 times, rescued only by an external reset at a[847]. `UID0215` a[88]-[264] then three compaction restarts. `UID0100` a[24]-[206]: a failing relative-path sed alternating with the absolute path 46 times. `UID0188` a[68]-[344]: one rg 139 times; `UID0188` b[49]-[654]: the same two-`find_tables` command 297 times. `UID0165` a[14]-[545]: a 13-step block ~41 times. `UID0114` a[65]-[849]: a 14-command cycle ~30 times for 55 minutes, never submitted; `UID0114` b[64]-[239]: one show_table ~88 times; `UID0114` b2[46]-[201]: a 48-cell printings loop (~78 KB per run) ~45 times. `UID0183` a[59]-[116] and a[165]-[278]: one rg 29 times, another 57 times; `UID0183` b[87]-[187]: one sqlite heredoc 33 times. `UID0029` b[70]-[346]: one rg pipeline 139 times. `UID0037` a[45]-[166] and a[189]-[341]: ~135 identical commands. `UID0245` a[40]-[250]: a 5-command cycle 22 times. `UID0211` a[69]-[145]: one 5-call command 39 times.

Smaller repeats (3-12x) that a note or self-correction ended: `UID0027` b[17]-[21] (identical calc x3), `UID0225` b[44]-[54] (rg x3), `UID0243` a[60]-[70] (six commands re-run verbatim) and b[56]-[60], `UID0214` b2[55]-[69] (two commands x3 each), `UID0213` c[48]-[50] (rg x3), `UID0037` b[25]-[29], `UID0030` b[37]-[45], `UID0188` b2 (one sed x8, one rg x4).

The AGENTS.md sentence "Loops: a command repeated with the same output will not change" is rated across the 50 `text_verdict` lists as **ignored 15, irrelevant 34, kept-and-helped 0**. Every cell that looped and had the sentence ignored it (`UID0215`, `UID0100`, `UID0030`, `UID0188`, `UID0165`, `UID0114`, `UID0183`, `UID0245`, `UID0211`, `UID0243`, `UID0037` for a; `UID0214`, `UID0188`, `UID0114` for b2; `UID0213` for c). Text does not stop loops.

### 2.2 The advisory loop guard (b, b2)

Harness inserts counted from the digest headers: b received **21 loop notes in 9 cells** (`UID0225`, `UID0030`, `UID0188` x2, `UID0114` x4, `UID0183` x7, `UID0027`, `UID0243`, `UID0029` x3, `UID0037`); b2 received **7 notes in 3 cells** (`UID0214` x2, `UID0188` x3, `UID0114` x2).

Where it worked (the repeat stopped after the note): `UID0030` b (note at call 21 ended a 3x cycle; b submitted at b[48]; 156 s vs a's 3272 s on the same question), `UID0225` b (note at call 26, then `--keep`), `UID0027` b (note at call 12 after identical calc x3), `UID0243` b (note at call 28, then `--keep`), `UID0037` b (note at call 14 ended a 3x repeat), `UID0214` b2 (notes at calls 32-33; b2[70] "We need stop loops"). Five of nine b cells, one of three b2 cells (plus `UID0188` b2, where the third note was followed by two more repeats and then a submit).

Where it failed: `UID0114` b (note at call 33 named the exact show_table command; the model repeated it ~85 more times; `loop_notes_max` 3 exhausted; compaction at call 122), `UID0114` b2 (note at call 27 named the printings loop; repeated ~40 more times; compaction at call 77), `UID0188` b (notes at calls 31 and 339; ~294 identical runs in between drew no note), `UID0183` b (7 notes; the flagged sqlite command re-run 30 more times after the third; the compaction splice at call 77 is what broke the loop), `UID0029` b (notes at 19, 27, 32; the command noted at 32 then repeated ~136 more times with no further note because `_loop_guard` skips commands already in `self.noted`).

Pattern: the note ends a *short* repeat of a single command when the model still has something else to try; it does not end a degenerate state in which the model re-runs a known-empty search, and the cap means a model that ignores it three times is never told again.

### 2.3 Why the loops happen (from the analyses)

- The tool answers a lookup for a value that does not exist with a generic "no matching row/column found" (`UID0114`: the series was discontinued; every cell had already read the discontinuation note at a[50], b[33], b2[26], c[57] before looping). The message reads like a syntax mistake, so the model retries.
- An external series (CPI, FX, silver price, chart bars) is absent from the corpus and the model searches the same places again (`UID0214` b2, `UID0165` a, `UID0243` a, `UID0188` a/b/b2, `UID0183` a/b, `UID0037` a, `UID0030` a).
- A show_table view returns an empty page that gives nothing to break on (`UID0245` a: `--from-row 12 --grep Aug` printed "page rows 0-0 of 12" 22 times; `UID0100` a: relative-path sed exit 1 alternating with the absolute path).
- Re-reasoning the same mapping (`UID0027` b: identical calc x3; `UID0029` b[30]-[41]).

Correctness effect: of a's 9 runaways, 7 are on questions every cell got wrong anyway (section 8), and the two right ones (`UID0100`, `UID0245`) had computed the right value before looping (`UID0100` a[21], `UID0245` a[36]). `UID0029` b was right after 1874 s. So loops are a **cost** symptom of an unfindable value, with one grade loss: `UID0114` a timed out at 3600 s with an empty answer.

### 2.4 Decision

**KEEP a loop guard; CHANGE it from advisory to enforced, cycle-aware and uncapped, and apply it to the text arm too.** Concretely (as the `UID0114`, `UID0188`, `UID0183` and `UID0029` analyses specify): when a command's text and its output hash equal a previous call's, do not re-execute; return the first ~20 lines of the cached output plus one line `[harness:loop] not re-run: identical to call N (k repeats, output unchanged); run a different command or submit`; after the 5th suppression in a row, insert "you have re-run 5 commands with known output; submit the best candidate now or state in one line what value is missing"; detect repeated *cycles* (the same ordered set of 3-14 commands), not only single commands; remove the `loop_notes_max` cap and the `self.noted` skip. Pair it with the tool-side fixes that re-open hunts (end-of-series message in `printings`, non-empty explanation on 0-row `show_table` pages, "figure caption, no values" line for charts), which belong to the TOOLS synthesis. Evidence: a 9/50 runaways, 90 % of tokens; b 4/50 despite 21 notes; c 0/50; the text sentence 0 kept-and-helped in 50 analyses.

---

## 3. Context compactions

Index counts (b/b2 only): b 4 (`UID0188`, `UID0114`, `UID0183`, `UID0029`), b2 1 (`UID0114`), each followed by a ledger splice ("Ledger recorded by the harness from tool outputs; authoritative where a summary disagrees"). All five occurred inside runaway runs. Codex-native compactions in a are not counted in index.json but the analyses report them in 8 cells: `UID0215` (three restarts), `UID0100`, `UID0030` (a[847]), `UID0211`, `UID0165`, `UID0183` (a[117], a[131], a[279]), `UID0245`, `UID0037` (a[167]).

What compaction did to answers:
- Never turned a wrong answer right or a right answer wrong by itself (no analysis attributes a flip to it).
- Locked in a wrong source: `UID0215` a's three post-compaction summaries each already said "PDO-3", and one carried a copy error in an intermediate (a[701]).
- Carried a wrong structural belief: `UID0029` b's compaction summary said the table covered only the last few years (b[347]); b[376] overrode it by counting the partial panel.
- Cells answered *from the summary*: `UID0100` a (right), `UID0211` a (wrong, computed before the loop), `UID0165` a (wrong), `UID0245` a (right, and re-cited the calc/provenance advice after the reset).
- The ledger splice content: `UID0188` b's splice listed the last-touched cells, an import-quota table from a 1939 issue, i.e. noise for the question; `UID0183` b's splice showed rows of a 1940s foreign-holdings table with an "India" row, which sent b to inspect irrelevant rows (b[218]-[219], b[231]-[232], b[263]-[264]) although the splice is also what broke its loop; `UID0114` b/b2 re-searched and then submitted.

**Decision: KEEP the ledger splice (5 uses, 0 grade harm) but CHANGE its selection**: key the splice to the question's nouns and to the shape facts the model needs (table id, row/column layout, YEAR_UNKNOWN, siblings, values already extracted), not "latest touched first". Treat compaction as a symptom: every compaction in the pilot followed a loop, so the enforced guard above is the control that matters.

---

## 4. Submit gate: findings and offers (b, b2)

### 4.1 Totals

b: gate rounds or more than one submit on **27 of 50** questions (36 gate rounds). b2: **2 of 25** (`UID0007`, `UID0114`; 3 rounds).

Did any gate round change the submitted answer? (first vs last submit payload; values not reproduced)

| cell | question | rule(s) shown | offer? | model action | answer change | direction |
|---|---|---|---|---|---|---|
| b | `UID0122` | competing_candidates (its own `--alt`); grounded and row_coverage hidden by 1-per-round | yes | accepted 3 s later, no new evidence (b[38]) | yes | wrong -> wrong (different wrong) |
| b | `UID0097` | competing_candidates (own `--alt`) | yes | accepted after a 108 s re-deliberation (b[17]-[18]) | yes | **right -> wrong** |
| b | `UID0007` | later_printing (bound to a receipts-panel row, not an expenditures cell) | yes | accepted although b[28] saw the panel mismatch ("Harness says latest wins") | yes | wrong -> worse wrong (~0.1 % -> ~2.6 %) |
| b2 | `UID0007` | later_printing (same wrong-panel binding) | yes | neither accepted nor kept; rebuilt all 80 cells from the later, re-based vintage (b2[22]-[32]) | yes | wrong -> wrong |
| b | `UID0214` | r1 competing_candidates + recompute_final on a shell-garbled payload; r2 competing_candidates | yes | r1 fixed the quoting; r2 kept | no (net) | unchanged (the r1 catch was useful) |
| b | `UID0211` | competing_candidates (own `--alt`); recompute_final on the accepted value suppressed | yes | accepted, citing "the harness computed that alternative itself" (b[65]) | yes | **right -> wrong** |
| b | `UID0165` | scale (false positive: bound the answer to a different number printed by the same calc); grounded on the normal-quantile constant | yes | accepted (b[50]-[51]) | yes | wrong -> wrong |
| b | `UID0114` | competing_candidates, recompute_final x2 | yes | 3 submits | precision only | wrong -> wrong |
| b2 | `UID0114` | answer_count rejected "not computable" (no offer); recompute_final | no | fabricated an estimate two calls later (b2[262]-[266]) | prose -> number | wrong -> wrong |
| b | `UID0117` | flagged_cells on a *correct* repaired cell shown first (with a misleading `--row 1976` hint); grounded (the real error) shown only in round 2 | no | kept with a prose reason after its own rg found no printed line (b[44]-[46]) | no | unchanged (wrong) |
| b | `UID0245` | scale ("100 times the output of calc"), recompute_final hidden | yes | accepted in one step (b[37]) | yes | **wrong -> right** |
| b | `UID0027` | recompute_final (hand-encoded two-digit year); loop note earlier | no | re-ran the encoding in calc with the four-digit year | yes (encoding) | wrong -> wrong (blind to the period error) |
| b | `UID0028` | competing_candidates (own `--alt`) | yes | accepted (b[94]); moved from the right table family to the detail table | yes | wrong -> wrong |
| b | `UID0243` | grounded on the question-sourced external CPI input; table_alternatives matched on "adjust" | no | ~10 more futile search steps, loop note, kept twice | no | unchanged (right) |
| b | `UID0148` | flagged_cells bound the count literal to a PDO-3 cell b never used; grounded on an input from a truncated page | no | kept twice | no | unchanged (wrong) |
| b | `UID0225` | table_alternatives attributed the answer to a table b never used (AY-1) | no | 17 steps decoding it, rg x3 -> loop note, kept | no | unchanged (right) |
| b | `UID0228`, `UID0044`, `UID0204`, `UID0037` | recompute_final | no | re-ran in calc, same value | no | unchanged (3 right, `UID0037` wrong) |
| b | `UID0155` | row_coverage (the "unused" cell is a repeated-value column; aggregate is a geometric mean) | no | kept | no | unchanged (wrong) |
| b | `UID0183` | grounded (a derived FX literal, correctly flagged) + table_alternatives (TSO-2 via a line b merely read) | no | kept; 4 steps of confusion | no | unchanged (wrong) |
| b | `UID0077`, `UID0213` | grounded on a `range(1,13)` loop bound / on a hand-typed CPI input | no | kept (`UID0213`: ~470 of 597 s in the round) | no | unchanged (right) |
| b | `UID0057`, `UID0230` | flagged_cells (ROWSPLIT rows; `UID0230` bound to a 1961_01 row b never used); `UID0230` also table_alternatives (part 1 of the same table) | no | kept | no | unchanged (`UID0057` right, `UID0230` wrong) |
| b | `UID0102`, `UID0029` | flagged_cells (Y? / capsule rows; `UID0102` bound to a 2017 capsule cell) + grounded (`UID0102`: in-head unit conversion; `UID0029`: a header line with no numeric cell) | no | kept twice | no | unchanged (`UID0102` wrong: the real 13-input error was not seen; `UID0029` right) |
| b | `UID0094` | row_coverage (7 "unused" cells of a row used for a 2-cell ratio) | no | kept | no | unchanged (right) |

**Tally for b (27 gate questions):** answer substantively changed in 7: **1 wrong->right** (`UID0245`), **2 right->wrong** (`UID0097`, `UID0211`), 4 wrong->wrong (`UID0122`, `UID0007`, `UID0165`, `UID0028`); 2 format/encoding-only changes (`UID0114`, `UID0027`); 18 unchanged. Net effect on b's score: **-1**. Offers were made on ~10 rounds and accepted 7 times; of the accepted offers 1 helped, 2 hurt, 4 replaced one wrong value with another.

**Tally for b2 (2 gate questions):** 2 changes, both wrong->wrong. Net 0. In 25 questions no b2 finding improved an answer.

**Silent gates that should have spoken** (rule did not fire although its case existed): later_printing on `UID0155` b2 (a later printing differed; had it fired and been accepted b2, the only right cell, would have been wrong), `UID0133` b (the revised 2013_12 reprint is untitled, so the ledger never linked it), `UID0062` b (the series continues under a renamed title, invisible to the title-keyed lookup); recompute_final when no calc was run at all (`UID0224` b, `UID0225` b, `UID0149` b, `UID0188` b, `UID0057` b) and once on a value no calc printed (`UID0240` b, probable substring match on a short numeral); grounded on memory-sourced CPI literals (`UID0214` b) and on assumed integers that happened to occur in narrative excerpts (`UID0037` b[59]).

**Gate read as an oracle:** five mechanism cells submitted *in order to see* whether a finding would appear: `UID0030` b2[40] ("submit guesses and see"), `UID0037` b[55] ("submit and see finding"), `UID0240` b[25] ("submit check may print finding if wrong"), `UID0148` b[41] ("use submit to test"), `UID0122` b[35] (planned `--alt` as a key probe). A silent `recorded` status was then read as verification (`UID0133` b: recorded silently with a stale vintage).

### 4.2 Rule by rule

| rule | fired (visible) | answers changed | helped | hurt | false / irrelevant fires | decision |
|---|---|---|---|---|---|---|
| competing_candidates (echoes the actor's own `--alt` as an Offer with an Accept line) | 6 cells (`UID0122`, `UID0097`, `UID0214` x2, `UID0211`, `UID0114`, `UID0028`) | 5 | 0 | 2 right->wrong; `UID0028` moved b off the right table family | every fire carried no information the actor did not already hold | **DROP**, together with the submit-note sentences "add `--alt` when you also considered another value" and "the harness computed that alternative itself" (read as an endorsement at `UID0211` b[65], `UID0028` b[94]; b2 still carries the sentence, harmless only because no offering rule remained). b2 ran without the rule and was right on `UID0097` and `UID0211`. |
| later_printing (Offer built from the "latest printing" of a used cell) | 2 (`UID0007` b and b2) | 2 | 0 | 2 wrong->wrong | both bound a receipts/surplus-panel row of a three-panel table as the "latest printing" of an expenditures cell; the finding text "latest wins unless a date is pinned" was read as an order (b[28], b2[22]); 3 silent cases where the intended situation existed (`UID0155` would have hurt; `UID0133`, `UID0062` would have helped) | **DROP from the gate until `printings` keys cells by (part, panel stub, section, period, column) and follows renamed/untitled reprints**; even then suppress when the later table's row-year span differs or the substitute differs by more than any revision seen; never as an Offer on a question that names an issue or as-of date (`UID0112`, `UID0057` show the exception clause doing the work). "Latest printing" must stay an advice, not an automatic offer. |
| scale (answer is 10^k times a calc output) | 2 (`UID0165` false, `UID0245` true) | 2 | **1 wrong->right** | 1 wrong->wrong | `UID0165`: bound the answer to a different number on the same calc line, offered it, accepted | **KEEP, CHANGE**: bind only when the answer is not already equal to (or a stated rounding of) a printed output of that calc; extend to a literal 100/200 factor inside the calc source (would have reached `UID0245` c, whose x100 lived inside the expression, c[29]). |
| recompute_final (answer is not an output of any calc) | 9 (`UID0228`, `UID0214`, `UID0211` suppressed, `UID0114` b/b2, `UID0044`, `UID0027`, `UID0204`, `UID0037`) | 2 (format/encoding) | 0 flips, but caught a shell-garbled payload (`UID0214` b[46]) and forced a hand-encoding into calc (`UID0027`) | 0 | does not fire when *no* calc was ever run (5 cells) or on a short numeral (`UID0240` b) | **KEEP** (cheap, 0 harm). CHANGE: never suppress it for an Accepted value (`UID0211` checks/002); make "no calc at all" a note; match whole tokens, not substrings. |
| grounded (a calc input no tool printed) | 10 (`UID0183`, `UID0117`, `UID0077`, `UID0102`, `UID0243`, `UID0213`, `UID0148`, `UID0029` visible; hidden in `UID0122`, `UID0165`) | 0 | 2 legitimate: `UID0117` (a hand-summed OCR row; ranked second and dismissed by a prose `--keep`), `UID0183` (an external peg; correctly kept) | 0 grade harm; cost `UID0213` b ~470 s, `UID0243` b ~13 requests | fires on a `range()` bound (`UID0077`), an in-head unit conversion (`UID0102`), a header line with no cell (`UID0029`), a math constant (`UID0165`), question-sourced external inputs (`UID0243`, `UID0213`), an input from a truncated page (`UID0148`); silent on memory-sourced CPI (`UID0214`) and assumed integers matching narrative numerals (`UID0037`) | **CHANGE**: no-offer note only; rank first; bind literals to a cell/line via the ledger's observe records, not by substring; whitelist constants, loop bounds and inputs the question names as external (or let the actor declare `--external "<source>"`); a `--keep` must cite a file:line whose text contains the value. |
| flagged_cells (used cell sits on a repaired / Y? row) | 6 (`UID0117`, `UID0057`, `UID0102`, `UID0148`, `UID0029`, `UID0230`) | 0 | 2 legitimate warnings on already-right answers (`UID0057` ROWSPLIT, `UID0029` Y? -> sibling check) | 0 | 3 mis-bindings to tables/issues never used (`UID0102` 2017 capsule, `UID0148` PDO-3, `UID0230` 1961_01); `UID0117`'s hint `--row 1976` came from the OCR-garbled stub | **DROP from the gate**; `show_table` already prints these flags where the model reads the value (the TOOLS synthesis owns the repair-line proposal from `UID0007`). |
| table_alternatives (another table in the issue matches more question words) | 4 (`UID0225`, `UID0183`, `UID0243`, `UID0230`) | 0 | 0 | cost 17 steps + a loop note on `UID0225`, 4 steps on `UID0183` | all four false: a table never used, a line merely read, the word "adjust", part 1 of the same table | **DROP** (b2 already dropped it). A value-disagreement sibling check is a tool-level idea (`UID0130`), not a gate rule. |
| row_coverage (a sum uses k of n cells of a row) | 3 (`UID0122` hidden, `UID0155`, `UID0094`) | 0 | 0 | 0 | all false: the "unused" cell is a repeated-value or total column, or the aggregate is a ratio / geometric mean | **DROP** (b2 already dropped it); if ever revived, gate it on the calc's operator. |
| answer_count | 1 (`UID0114` b2) | 1 (prose -> number) | 0 | pushed an honest non-answer into a fabricated estimate | — | **KEEP the format check, CHANGE the behaviour**: never bare-reject after an end-of-series/discontinuation message in the trajectory; offer the last printed value or ask for a stated proxy. A format check has value: `UID0114` c returned a prose sentence graded `count_mismatch(3 vs 1)` and `UID0114` a timed out with an empty answer. |
| period_match | 0 in 75 cell-runs | — | — | — | blind to cells parsed inside calc from raw files (`UID0027` b, `UID0102` b's 13-input window) | **DROP** (or fold into the tool's period labels / an FY-aware filter). |
| `gate_findings_per_round` = 1, `gate_rounds_max` = 2 | — | — | — | hid grounded behind competing_candidates (`UID0122`) and behind flagged_cells (`UID0117`); hid recompute_final behind scale (`UID0245`); a `--keep` drew a *different* rule's finding instead of being recorded (`UID0230`) | — | **CHANGE**: show all findings as no-offer notes in one round; at most one round; record a `--keep` of an unchanged answer. |
| silent `recorded` status | — | — | — | acts as a false all-clear (`UID0133` b) and invites probing (5 cells above) | — | **CHANGE**: the submit note must say the gate is a format/provenance check, not a grader; print "recorded (no checks failed; this is not a correctness verdict)". |

### 4.3 What the mechanism arm added, net

- b2 = a's text + submit + 5 rules: 13/25 vs a's 15/25 on the same questions. b2's five losses vs a: `UID0122` (a `--long` view hid the group header; tool), `UID0220` (table choice), `UID0007` (**gate later_printing**), `UID0109` (summed an extra column; both mechanism cells did, though b2 had a's text), `UID0182` (search luck). b2's three wins vs a: `UID0180` (a tailed the %g-rounded printings summary; b2 read exact integers from show_table), `UID0155` (first printing vs latest printing, issue-47; b2 right by a truncated printings sweep), `UID0211` (same-title table tie). Only one of these eight differences is a control effect (`UID0007`), and it is negative.
- b vs b2 on 25: b2 +4. Two of the four (`UID0097`, `UID0211`) are pure removals of competing_candidates.
- Across 75 mechanism cell-runs, the gate improved exactly one answer (`UID0245`, scale) and worsened two. Everything else it did was cost: the `UID0097` post-finding turn took two thirds of the episode's wall time; `UID0225` lost 17 steps; `UID0243` ~13 requests; `UID0213` ~470 s; `UID0230` ~22 steps; `UID0019` b spent 568 s and 76 K output tokens re-deriving a parse defect that the printings summary already stated (the most expensive right answer in the pilot, b[29], b[36]).

### 4.4 Decision for v10 controls at submit time

Keep `submit` as the answer-capture mechanism (format validity, one value, no prose) and keep two cheap checks with offers restricted to values the harness itself computed from a calc sha: **scale** (repaired) and **recompute_final**. Drop every rule whose only input is the actor's own alternative or a word-overlap heuristic. Any remaining finding is a one-round, no-Accept-line note. The `UID0007` and `UID0211` analyses state the principle: an Offer must come from evidence the actor did not already hold, and must carry a non-empty `compute_sha256`. Tell the actor, in the submit note, that a silent record is not a verdict.

---

## 5. Runaway outputs, the 8000-byte cap and silent-empty output

Questions whose analysis names an output cut, flood or truncation as friction in at least one cell: **41 of 50** (all but `UID0097`, `UID0030`, `UID0007`, `UID0188`, `UID0183`, `UID0245`, `UID0213`, `UID0230`, `UID0037`, which had other frictions). Recurrent shapes:

1. `find_tables --all` where one large group's part list fills the page and hides other groups (`UID0180` b: 314 FD-1 issues hid OFS-1; `UID0243` a and c: 1 of 162 groups shown, the decisive summary table never listed; `UID0133` a: the 1982-2001 group hid the 2002-2013 group; `UID0130` c: TSO-1 at rank 14 near the cut; `UID0220` b2/c: four component tables hid the summary table; `UID0057` c: the last two January issues clipped).
2. `printings` listings flooded by daily/weekly rows, multi-year loops or several ids for one column, cutting the SUMMARY that carries the decision (`UID0224` every cell; `UID0109` a ~24 KB omitted; `UID0114` b2 ~78 KB per run; `UID0122` 18-30 tailed calls per cell; `UID0155` a/b2/c middle-truncated sweeps, which is how b2 accidentally landed on the first printing; `UID0135` a/c; `UID0240` all cells; `UID0062` a lost the REVISED line; `UID0243` c). Models coped with `tail -n 4/8`, which is exactly how `UID0180` a lost (the tail exposed the %g-rounded summary).
3. `rg ... | head -100` / `-m 10` hiding the narrative hits that would have solved `UID0215` (b[8], b2[44],[48]); `docs.read`/`rg -C` floods of 30-47 KB on `UID0204` b/c.
4. Multi-query commands losing the first query's output (`UID0182` b[8], `UID0180` c[6], `UID0112` a[33], b2[11]).
5. Wide tables paging 7-9 rows per 8000 bytes under `--long` (`UID0225`, `UID0044` a[9], `UID0094` c[13]-[16], `UID0148` a[13]), `--long` listing every cell as a row (`UID0027` c[15], `UID0029` c[26]).

Silent-empty output (exit 0, 0 bytes) is a separate control problem: `UID0154` b2[13] and `UID0170` c[14] (both reproduce correctly offline: a harness capture loss), `UID0112` b2[13] (plain `show_table <file> <line>` with no options prints nothing: tool default), `UID0096` c[33], `UID0220` a[18], `UID0148` a[17] (printings/show_table printed nothing). No cell drew a wrong conclusion from the silence this time; a weaker run would read it as "no rows".

Decision: **KEEP the per-command cap (it bounds context growth) but CHANGE what is cut and ADD an empty-output guard**: groups before parts (never cut a group line), SUMMARY before listing in printings, a per-query budget when several tools run in one command, an explicit "OUTPUT CUT at N bytes: M more lines; use --years / --summary" trailer; and when a harness tool command exits 0 with 0 bytes, re-run it once and, if still empty, insert `[harness] <cmd> printed nothing (exit 0); output lost, run it again`. The tool-side restructuring belongs to the TOOLS synthesis; the control is "what survives a cut" and "silence is reported".

---

## 6. Temperature (c vs a)

c 24/50 vs a 31/50 with identical text. a right / c wrong: 11 (`UID0122`, `UID0220`, `UID0097`, `UID0130`, `UID0007`, `UID0182`, `UID0245`, `UID0027`, `UID0028`, `UID0133`, `UID0237`). c right / a wrong: 4 (`UID0180`, `UID0101`, `UID0211`, `UID0149`).

Where c lost, the first divergence is a one-step judgment that a's T=0 default happened to get right: the row modifier "nominal" (`UID0097` c[11], c[30]), a table pick from a listing (`UID0130` c[7], `UID0220` c[9], `UID0182` c[7], `UID0133` c[15]), x100 inside the calc (`UID0245` c[29]), a two-digit year (`UID0027` c[44]), a units phrase over the metric noun (`UID0237` c[23]), a receivable under a header (`UID0122` c[41]), skipping the FAILS-row check (`UID0007` c[19]), precision over wording (`UID0028` c[100]). Where c won, the sampled path avoided a trap (`UID0180`: ran printings untruncated; `UID0101`: printed both definitions and chose the parenthetical, c[36]-[39]; `UID0211`: same-title tie, c[36]; `UID0149`: expanded the acronym and opened the by-issues table).

Other c facts: 0 runaway runs and one ninth of a's tokens (loops are a T=0 artifact); two format losses of its own (`UID0114` prose answer -> `count_mismatch`, `UID0245` `scale_10^2`); one `transport_retry` (`UID0228`, 561 s for 48 requests: provider latency). The earlier baseline-variance audit found that T=0 runs are not deterministic across forks either, so single-question a/c differences on one-step judgments are partly noise; the aggregate (-7) is not.

Decision: **KEEP T=0 for the scoring arm** (comparability with a/b/b2 and with v8), do not use T=1 as the loop breaker (the enforced loop guard does that without the -7), and reserve sampled forks for the evolver's exploration of *candidates*, not for evaluation.

---

## 7. Issue-47 and ambiguous questions

Flagged issue-47 in index.json (5): `UID0154` (all cells right; the find_tables as-of tag carried it), `UID0007` (a right; key follows the repaired in-range values, so not irreducible), `UID0155` (only b2 right, by luck), `UID0237` (a right; answer-form dispute), `UID0213` (all right; annual-average CPI reading accepted).

`UID0155` deserves a note because it inverts the pilot's main advice: a, b and c ran `printings` on the four July cells and took a later revision of one 2011 cell; b2's printings loop was truncated, so it read the 2011 row from the September issue (first printing) and was the only right cell. a's and c's error bins are `within_1pct`. b2's own later_printing rule stayed silent, which is the only reason its luck held. Whatever the key intends, a control that *forces* the latest printing (later_printing offers) would lose this question every time; and `UID0135`, `UID0133`, `UID0019` show the opposite (latest printing graded right, reference's single-issue or "more precise" reading graded wrong). The convention must stay an advice with the "unless the question names an issue or date" clause, which a[26]/b2[20]/c[23] applied correctly on `UID0112` and a[32] on `UID0057`.

Questions the analysts mark partly or wholly ambiguous although not flagged: `UID0122` (receivable under a header without subtotal), `UID0220` (percent difference vs percent change; the reference is graded wrong with the same source), `UID0097` ("nominal"), `UID0130` (a one-unit tie between two legitimate tables; the reference and its reviewer chose the other), `UID0101` ("arc elasticity" with one series; reference wrong), `UID0214` (key's reading not chosen by any of eight solvers), `UID0211` (two same-title tables; reference wrong), `UID0165` (metric convention + external FX), `UID0114` (missing "actual"), `UID0245` (bare fraction vs percent), `UID0100` (index origin; all resolved it the same way), `UID0062` ("reporting structure" phrase read as a vintage pin by all 7 arms and the reference), `UID0204` (sentence to one decimal vs table cell; the literal reading is graded right, the "more precise" reference wrong), `UID0135` (single issue vs latest revisions), `UID0182` (CM-I-1 vs CM-III-1 once both are visible), `UID0102`, `UID0230`, `UID0240` (rounding / annualization / formula conventions no arm reproduced), `UID0096` (qualifier narrower than the total row; reference right).

Decision: no control can settle these. For the evolver, mark the five issue-47 questions and every question where the reference solution is graded wrong (`UID0220`, `UID0130`, `UID0101`, `UID0155`, `UID0211`, `UID0182`, `UID0148`, `UID0214`, `UID0165`, `UID0114`, `UID0019`, `UID0135`, `UID0204`, `UID0030`, `UID0188`, `UID0183`, `UID0102`, `UID0062`, `UID0230`, `UID0240`, `UID0037`) as low-signal for fitness, and never let a submit rule *offer* a change on them: the gate's only two right->wrong flips were on such one-step judgments (`UID0097`, `UID0211`).

---

## 8. Questions where every cell was wrong (13)

All 13 now have a per-question analysis.

| task | what happened | what it would take | irreducible? |
|---|---|---|---|
| `UID0215` | all four took table PDO-3; the source is a narrative section ("Treasury Financing Operations") that `find_tables` does not index; b and b2 grepped it and lost the hits to `head -100` / `-m 10` (b[8], b2[44],[48]); a looped 900 steps | TOOL: index narrative section headings and print `[section]` hits (fixes all four per the analysis); enforced loop guard for a | no (reference right, high confidence) |
| `UID0030` | count of local maxima on a printed page's line plots; the corpus keeps only chart titles, ticks and annotations; five solvers, five different counts; a looped ~137 cycles (3272 s, 56 M tokens), b's loop note ended the same behaviour at call 21 (156 s) | CONTROL: loop guard in every arm (cost only); TOOL: "figure caption, no values" line; an "unverifiable" gate path so a guess is recorded as such | yes |
| `UID0214` | all eight solvers (4 cells, 3 v8 arms, reference) chose the same reading and are within ~1 % of each other; key 5-25 % away; CPI-U absent from the corpus and recalled four different ways | nothing supportable; a reference-series tool would reproduce the reference's wrong answer | yes |
| `UID0188` | silver monetary stock at statutory value found by every cell; the market/real silver price does not exist in the corpus; a 352 / b 677 steps in loops (a: one rg 139x; b: 297x) | CONTROL: hard loop breaker (cost); TOOL: column-index-consistency warning in printings (b2[21] took a mis-labelled column) | yes |
| `UID0165` | header shift put every cell on the wrong OFS-2 column except c; the FRED-style FX rate is not in the corpus; VaR convention unpinned; reference wrong | TOOL: realign labels when the header has k+1 labels for k columns (worth doing for other OFS-2 questions); the grade itself is out of reach; a's 573-step loop needs the guard | largely yes |
| `UID0114` | the "actual" Jan 2003 value does not exist (series discontinued); a looped 55 min and timed out; b/b2 looped and fabricated; c answered prose | TOOL: end-of-series message; CONTROL: enforced loop guard, answer_count with an offer path; the key's convention is unknown | yes for the grade |
| `UID0183` | the corpus value cancels; the answer is the external 1966 INR/USD annual average under a convention the reference also missed; a 306 / b 270 steps in loops (7 loop notes ignored) | CONTROL: hard loop break with a "corpus exhausted" note (cost); the grade is unreachable | yes |
| `UID0102` | data step solved by all (12 FY2021 months; b took 13); the reference's reading and both tie-rounding variants of it are graded wrong, so the key uses a reading none of 7 arms produced | TEXT: "a fiscal year has exactly 12 month rows" (fixes b's divergence only); an FY-aware show_table filter | yes for the grade |
| `UID0062` | all three cells, all three v8 arms and the reference read the "reporting structure" phrase as a vintage pin and used the unrevised FY1947 value; b/c never saw the revised printing because the series was renamed and printings' title match stopped at 2 issues ("same in all printings") | TOOL: printings follows a series across a title change and prints the revision footnote next to an `r` value (would fix b, c, likely a); TEXT: "a phrase describing a column does not pin the vintage" | partly (systematic convention miss; the key's value is inferred from the error bin) |
| `UID0096` | all cells used the grand-total rows although the question's qualifier excludes a footnoted "Free list" component; the hypothesis was voiced (a[34], b[37]) with the footnote on screen and never computed; a also answered as a percent | TEXT: "a qualifier narrower than a total row: subtract the footnoted component from every operand and compute both candidates" + "a rate with no percent cue is a decimal" (fixes b, c, a) | no (reference right) |
| `UID0230` | two 26-week rates extracted identically by all; log return x 52 by all and by the reference; all `within_1pct` and wrong, so the key uses another annualization/return convention | nothing supportable; harness hygiene only (b's two findings were both false) | yes |
| `UID0240` | two cells retrieved identically by all; "implied Macaulay-style duration" under-specified; a and b `within_0.1pct` but not string-equal; c and the reference chose other readings | nothing; exclude from fitness | yes |
| `UID0037` | payroll-employment *chart* bars absent from the text; every solver recalled or substituted; a looped ~135 identical commands (22 M tokens) with no guard, b's note ended a 3x repeat at call 14 | CONTROL: loop guard in every arm (cost only); TOOL: "no table at this line; figure caption, no values" instead of jumping to the next table | yes |

Summary: **10 of 13 are irreducible** for the grade (`UID0030`, `UID0214`, `UID0188`, `UID0165`, `UID0114`, `UID0183`, `UID0102`, `UID0230`, `UID0240`, `UID0037`), **3 are fixable** (`UID0215` TOOL, `UID0096` TEXT, `UID0062` TOOL, partly). Seven of a's nine runaways sit in this table: the enforced loop guard would not have changed a single grade here but would have returned roughly 5 of a's 7.3 hours.

---

## 9. Irreducible questions (for the evolver's fitness weighting)

Wholly irreducible per the analyses (the grade cannot be reached from the corpus with any adapter change): `UID0030`, `UID0214`, `UID0188`, `UID0165` (largely), `UID0114`, `UID0183`, `UID0102`, `UID0230`, `UID0240`, `UID0037`, plus `UID0155` (issue-47: key follows an earlier printing; the only right cell was right by a truncated tool output). That is 11 of 50.

Partly irreducible (a genuine ambiguity or disputed key remains after the best adapter change): `UID0122`, `UID0220`, `UID0097`, `UID0130`, `UID0101`, `UID0211`, `UID0245`, `UID0237` (issue-47), `UID0062`, `UID0204`, `UID0135`, `UID0096`, `UID0182`, plus the residual `UID0100` index-origin coin flip.

Explicitly *not* irreducible although some cell or the reference lost them: `UID0180`, `UID0224`, `UID0225`, `UID0215`, `UID0100`, `UID0007`, `UID0109`, `UID0117`, `UID0027`, `UID0028`, `UID0149`, `UID0133`, `UID0243` (debt series; the CPI half stays luck), `UID0148`, `UID0019`, `UID0112`, `UID0154`, `UID0170`, `UID0228`, `UID0077`, `UID0057`, `UID0044`, `UID0213`, `UID0094`, `UID0029`, `UID0218`.

Two warnings the analyses attach for the learner: (1) on `UID0204`, `UID0148`, `UID0019` and `UID0135` the *reference* lost by being more diligent (a more precise table cell, a cross-vintage "repair" of a pinned cell, a more precise rate, a single-issue reading), so a fitness signal that rewards "find the exact table value" would flip right cells; (2) on `UID0155` vs `UID0133`/`UID0135`/`UID0019` the latest-printing convention cuts both ways, so it must never become an automatic offer.

---

## 10. Everything else worth carrying into v10 controls

- **Budget**: the 3600 s cap was hit once (`UID0114` a, empty answer). With an enforced loop guard the cap can stay; without it, a 30-minute cap would have saved ~5 of a's hours at no accuracy cost (all runaway-wrong cells were on all-wrong questions). Four `transport_retry` runs (`UID0154` a/b, `UID0228` c, `UID0188` b) are provider latency, not behaviour.
- **Output format enforcement** is the one cheap control with a demonstrated grade effect in both directions: `UID0114` c (prose -> `count_mismatch`) and `UID0245` (x100). A final "one value, no prose, no unit unless asked" check with an offer computed from the actor's own calc output is worth keeping in every arm, including text-only. Unit suffixes were tolerated by the grader on `UID0228` a, `UID0077`, `UID0044`, so the check should strip, not reject.
- **Provenance binding by literal value is unreliable** in a corpus full of near-identical numbers (`UID0148`: the count literal bound to a PDO-3 cell never used; `UID0225`: answer attributed to AY-1; `UID0102`: a tenths-rounded input bound to a 2017 capsule; `UID0230`: a rate bound to a 1961_01 row; `UID0037`: assumed integers "grounded" by narrative numerals). Any v10 rule that needs "which cell did the actor use" must bind through the ledger's observe records, scale-aware (x/1000, rounding), not by searching for the number.
- **Advice sentences the controls contradicted**: "latest wins unless a date is pinned" (finding text) overrode the actor's own reading of the question's scope (`UID0007` b[28], b2[22]); the submit note's Offer sentence overrode a right calc result (`UID0211`, `UID0028`). v10 text and v10 controls must not carry conflicting instructions.
- **Things that looked like harness effects but were tools**: the wrong-panel later_printing offers (`UID0007`), the fake REVISED/DIFFERS from two same-title tables (`UID0211`) or from four panels of one issue (`UID0044`), the fake revision from an OCR header shift (`UID0214`, `UID0228`, `UID0188`), the misleading `--row 1976` hint (`UID0117`), the silent "same in all printings" on a renamed series (`UID0062`), the unreachable untitled reprint (`UID0133`). All come from `printings` keying cells by (period, column label) only and by title tokens. Fixing that key (add part, panel stub, section, as-of date; follow renames and untitled twins) removes six gate false positives or silences at once; until then no offer should be built from `printings`.
- **What the mechanism arm cannot see** (analysts' phrase): table choice (`UID0220`, `UID0182`, `UID0133`, `UID0243` c), an extra column folded into an entity total (`UID0109`), a formula definition (`UID0101`, `UID0240`), an answer-form choice (`UID0237`), a cell parsed inside calc (`UID0027`, `UID0102`), a qualifier narrower than a total row (`UID0096`), an acronym read as a typo (`UID0149`). Every one of these is a "what the model saw" problem, which is why the analyses put 17 of 50 smallest changes at TOOL and 8 at TEXT.
- **Empty-output guard** (new, section 5): re-run once, then report the silence.
- **Gate-as-oracle** (new, section 4.1): five cells probed `submit`; the note must say a silent record is not a verdict, and the "unverifiable" path proposed for `UID0030` (no calc, no cell, question mentions a chart) should record the guess as a guess.
- **Ledger splice selection** (section 3): key to the question's nouns and table shape, not recency.
- **The mechanism arm is expensive even when right**: `UID0019` b 568 s / 76 K output tokens, `UID0213` b ~470 s in one grounded round, `UID0029` b 1874 s, `UID0225` b +17 steps, `UID0243` b +13 requests. A control that costs a round on a right answer needs a demonstrated catch rate; none of the dropped rules has one.

---

## 11. Decision table (one line per control)

| control | decision | evidence in one line |
|---|---|---|
| loop guard (advisory, 3 repeats, max 3 notes, once per command) | CHANGE -> enforced (do not re-execute), cycle-aware, uncapped, all arms | a 9 runaways / 90 % tokens with no guard; b 4 runaways despite 21 notes (`UID0029` b 136 repeats after the note); c 0 |
| "Loops" advice sentence | DROP from text (a control does this) | 0 kept-and-helped, 15 ignored, 34 irrelevant in 50 analyses |
| ledger splice after compaction | KEEP, CHANGE selection (question nouns + table shape, not recency) | 5 uses, 0 grade harm, 2 noisy/misleading splices (`UID0188`, `UID0183`) |
| submit command + format validity | KEEP (all arms), strip units rather than reject | `UID0114` a empty, c prose; `UID0245` scale |
| competing_candidates + `--alt` solicitation + "harness computed it" sentence | DROP | 5 answer changes: 0 helped, 2 right->wrong, 1 moved off the right table |
| later_printing | DROP until printings is panel/section/rename-aware; never as an Offer; never when an issue or as-of date is named | 2 fires, both wrong panel; 3 silent cases (`UID0155` luck, `UID0133`, `UID0062`); `UID0155` shows latest printing is not always the key |
| scale | KEEP + fix binding + inspect calc source for 100/200 | 1 wrong->right, 1 false positive accepted |
| recompute_final | KEEP; do not suppress after Accept; note when no calc at all; whole-token match | 9 fires, 0 harm, caught a garbled submit; 5 no-calc silences, 1 substring miss |
| grounded | CHANGE to no-offer note, ranked first, ledger-bound, with whitelists and `--external` | 10 fires, 0 changes, 2 legitimate (1 dismissed), ~6 false, 2 misses |
| flagged_cells | DROP from gate (keep in show_table output) | 6 fires, 0 changes, 3 bound to tables never used |
| table_alternatives | DROP | 4 fires, all false, 17 wasted steps once |
| row_coverage | DROP | 3 fires, all false (ratio, geometric mean, total column) |
| answer_count | KEEP with an offer path, no bare rejection | 1 fire -> fabrication (`UID0114` b2) |
| period_match | DROP | 0 fires in 75 runs; blind to calc-parsed cells |
| one finding per round / two rounds / `--keep` re-checked | CHANGE -> all notes in one round; record a `--keep` | hid the real finding 3 times; `UID0230` `--keep` drew a new false finding |
| silent `recorded` | CHANGE -> say it is not a verdict | 5 cells probed the gate; `UID0133` false all-clear |
| 8000-byte cap | KEEP cap, CHANGE what survives (groups, SUMMARY first, per-query budget, explicit trailer) | 41/50 questions cite cuts |
| empty-output guard | ADD (re-run once, then report) | `UID0154` b2[13], `UID0170` c[14] capture loss; 4 more silent-empty tool outputs |
| T=0 | KEEP for scoring | c -7 on one-step judgments; loops handled by the guard instead |
| 3600 s budget | KEEP, with the guard | hit once, on an irreducible question |
