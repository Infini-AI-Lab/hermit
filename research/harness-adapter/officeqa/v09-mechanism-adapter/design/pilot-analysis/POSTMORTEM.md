# v9 pilot postmortem: why mech-static (b) and T=1.0 (c) lost to text-static (a)

Sources: `index.json`, the 19 per-question analyses in `q/*.json` (all discordant questions of the a-vs-b and a-vs-c
pairs), `../../v9-p1-metrics.json` (official go/no-go), `../DESIGN.md`, the adapters in `v9-p1-{a,b}/adapters/`, and
our own recount over `v9-p1-b/runs/episodes/*/api/checks/*.json`, `summary.json` and `records/events.jsonl`. No answer
value appears in this file. Question ids are cited as `UIDxxxx`.

---

## 中文摘要（写给初学者）

**先解释几个词。**
- **cell（实验格）**：同一批 50 道 OfficeQA 题，用三种不同设置各跑一遍。a = `text-static`（温度 0，把约 2.5 KB 的
  “做题建议”作为文字放进 AGENTS.md）；b = `mech-static`（温度 0，同样的建议不写成文字，而是改成 10 条在提交时自动执行的
  “规则”）；c = 和 a 一样，但温度 1.0。
- **seed text（种子文本）**：a 和 c 在 AGENTS.md 里看到的那段建议，例如“每个表格单元格都用 `printings` 查最新版本”、
  “最后的数字一定用 `calc` 算”、“某行总数对不上（identity FAILS）时要和另一期刊物对照”。b 只看到一段 829 字节的
  “怎么提交”说明。
- **gate（提交闸门）**：b 做完题后必须运行 `submit`，harness（外部控制程序）检查答案，可能打印一条 **finding（发现）**，
  有时附带一个 **Offer（替代答案）**，模型选 **Accept（接受）** 或 **Keep（坚持原答案）**。
- **temperature（温度）**：控制模型采样随机性的参数。T=0 表示每一步都选概率最高的词（贪心、几乎确定）；T=1.0 表示按概率
  随机抽样，更“发散”。
- **discordant（不一致题）**：两个 cell 一对一错的题。只有这些题能说明两个设置的差别。

**结论 1：b（20/50）输给 a（31/50）的主要原因是“把文字建议拿走了”，而不是闸门本身。**
- 分解这 11 分：闸门前 b 的答案对 21 题，所以约 **10 分差距发生在提交之前**，闸门只再净扣 **1 分**（改错 2 题：
  UID0097、UID0211；改对 1 题：UID0245）。
- a 对 b 错的 12 题里：**6 题**是 a 明确用到了种子建议、而 b 恰好犯了那条建议要防的错（强证据 3 题：UID0007 对照另一期、
  UID0117 先打印原行、UID0220 选标题最贴合的表；中等 3 题：UID0109、UID0237、UID0027）；**1 题**是闸门把对的改成错的
  （UID0097）；**5 题**是搜索运气/歧义/噪声（UID0122、UID0182、UID0028、UID0133、UID0148，这 5 题 c 也几乎都错）。
- 旁证：a 的推理笔记在 22/50 题里直接引用 AGENTS/“instructions say”，43/50 题提到“latest printing”；b 分别只有 3 和 20。
  a 在 42/50 题用了 `printings`，b 只有 31/50。
- 闸门的副作用：`competing_candidates` 规则把模型自己的 `--alt`（备选答案）原样当作“Offer”返还，模型以为这是 harness
  算出来的“正确答案”，接受了 4 次，**4 次最后全错**（其中 2 次原本是对的）。有 6 题的笔记里模型明确打算“用 submit 试探
  答案”。
- 循环提示（loop notes）没有改变正确率（出现循环的 9 题，a 和 b 都各对 3 题），但显著省钱：b 总输入 token 约 128M，
  a 约 322M（a 有 10 题陷入重复命令循环，文字里的“不要重复”建议没拦住）。

**结论 2：c（T=1.0，24/50）没有循环，却输给 a（31/50）。** a 对 c 错 11 题，c 对 a 错 4 题，净 −7。
- **4 题是“验证变少、过早定案”**：c 跑得更短、少用 `printings`，跳过了 a 做过的关键检查（UID0007 请求数 10 对 26，
  UID0220 8 对 17，UID0133 16 对 27 且完全没查 printings，UID0130 直接用了排名靠前的相似表）。
- **4 题是“歧义题掷硬币掷反了”**：T=1 反而探索得更久，找到了第二种理解，然后选错（UID0097、UID0122、UID0237、UID0028）。
- **2 题是格式/公式小失误**（UID0245 乘了 100；UID0027 年份编码写成两位），**1 题是搜索路径运气**（UID0182）。
- c 赢的 4 题里 3 题是“探索碰巧对了”（UID0101、UID0149、UID0211），1 题是 a 自己截断了输出（UID0180）。
- 所以温度规则按预注册结果选 **T=0**，但 T=0 需要一个外部的循环保护。

**结论 3：各规则表现。** 只有 `scale`（百分数/小数倍数检查）的 Offer 真的对过一次（UID0245，错→对）。
`competing_candidates`：6 次展示 Offer，0 次正确，2 次对→错，**应删除**。`later_printing`：唯一一次 Offer 把“支出”
格子换成了同一行“收入”面板的格子（UID0007），**先关掉 Offer，修好版本对应关系后再放进工具里**。`flagged_cells`、
`table_alternatives`、`row_coverage` 从没改变过结果，却经常打扰已经对的题，**从提交闸门移除，改为工具输出里的提示**。
`grounded` 唯一一次真正抓到错误（UID0117），却排在一条无关提示后面，又被一句话“Keep”掉了，**要重写**（放在计算时检查、
外部数据如 CPI 豁免）。`recompute_final` 抓到过一个真实的编码错误（UID0027），保留，但不能在接受 Offer 后被静音。
`answer_count`、`period_match` 从未触发。

**结论 4：v10 的建议（每条都附“怎么验证”）。**
1. **所有 arm 都保留种子文字，机制叠加在上面**，而不是“用规则替换文字”。
2. **删除“接受你自己的备选”这种 Offer**，SEED 里也不再鼓励 `--alt`。
3. **把检查前移到选表/读格子的时候**（工具输出里直接标出 identity 失败的对照结果、按表+面板+行+列跟踪版本、给堆叠年份
   表输出带年份的行、`printings` 汇总不再四舍五入成 6 位有效数字）。
4. **在预注册任务约定适用时，让 harness 直接决定**（“除非题目指定日期，用最新修订版”“最大精度”“答案等于 calc 输出的
   100 倍且题目没要百分数时去掉 ×100”），而不是问模型“要不要接受”。
5. **T=0 + 所有 arm 都有 broker 级循环保护**（3 条提示用完后直接拦截重复命令）。
下一轮 pilot 建议 4 个 arm、全部 T=0、每个跑 2 次重复，见文末“Next pilot”。

---

## 1. Why b (mech-static, 20/50) lost to a (text-static, 31/50)

### 1.1 Accounting of the 11-point gap

| step | b right | source |
|---|---|---|
| b before any gate feedback (first submit) | 21 | metrics `right_episodes` = 21 (`judged_right`) |
| gate flips right→wrong | −2 | UID0097, UID0211 (`competing_candidates`, accepted own `--alt`) |
| gate flips wrong→right | +1 | UID0245 (`scale`, correct offer accepted) |
| b final | 20 | |

So about **10 of the 11 points** are lost *before* the gate ever speaks, and the gate costs **1 point net**
(it also changed the answer wrong→wrong in 7 more episodes: UID0122, UID0007, UID0214, UID0165, UID0114, UID0027,
UID0028).

Discordant pairs (final): a right / b wrong = 12, b right / a wrong = 1 (UID0243).

### 1.2 Cause for each of the 12 a-right / b-wrong questions

| question | first divergence (b) | primary cause | seed advice a used (and b lacked) | gate role | c (same seed, T=1) |
|---|---|---|---|---|---|
| UID0007 | EXTRACTION | **missing seed (strong)** | identity-FAILS row → compare with another printing | `later_printing` offer (wrong panel) accepted: wrong→more wrong | wrong (skipped the same check) |
| UID0117 | CELL_SELECTION | **missing seed (strong)** | "Before you rely on a value, print the line it comes from" | `grounded` caught the real error but was shown 2nd and dismissed by a prose Keep | right |
| UID0220 | SOURCE_TABLE | **missing seed (strong)** | "prefer the table whose title and column headers match"; a re-searched for a broader total | silent | wrong |
| UID0109 | CELL_SELECTION | missing seed (moderate) | "Alternatives … weigh them against the question's exact wording"; "decide for each column" | silent | right |
| UID0237 | INTERPRETATION | missing seed (moderate) | a quoted the percent/fraction bullet when choosing the ratio form | silent | wrong |
| UID0027 | CELL_SELECTION | missing seed (moderate) + execution | "Match the period exactly" (a filtered the window); b decoded argmax index wrongly and kept out-of-window months | `recompute_final` fixed an encoding slip, blind to the period error; 1 loop note, not helpful | wrong |
| UID0097 | INTERPRETATION | **gate (right→wrong)** | none decisive | own `--alt` echoed as Offer; b re-decided and accepted | wrong |
| UID0122 | CELL_SELECTION | noise / ambiguity (row membership) | none decisive | own `--alt` accepted as "answer key hint" (wrong→wrong) | wrong |
| UID0182 | SOURCE_TABLE | noise (search phrasing luck) | none decisive | silent | wrong |
| UID0028 | CELL_SELECTION | noise / hard multi-step (stacked-panel year map) | "prefer table whose title matches" helped a later, not decisive | own `--alt` accepted (wrong→wrong) | wrong |
| UID0133 | SOURCE_ISSUE_VINTAGE | noise / tool gap (latest copy of chart lost its caption) | latest-printing + literal-phrase table choice (unclear) | silent = false all-clear | wrong |
| UID0148 | CELL_SELECTION | noise / tool (stitched headers mislabelled) | unclear | `flagged_cells` bound to a cell of another table; two Keep rounds; planned oracle use | right |

Summary counts over the 12:

| cause | n | questions |
|---|---|---|
| missing seed advice, strong evidence | 3 | UID0007, UID0117, UID0220 |
| missing seed advice, moderate evidence | 3 | UID0109, UID0237, UID0027 |
| gate flip right→wrong | 1 | UID0097 |
| noise / search luck / ambiguity / tool gaps | 5 | UID0122, UID0182, UID0028, UID0133, UID0148 |

Outside the 12: **UID0211** (a and b both wrong) — b was right before the gate and the gate flipped it; **UID0245**
(both right) — b was right only because of the gate; **UID0243** (b right, a wrong) — a's own column/CPI error,
the gate only cost b about 13 requests.

Caveat: in v8, same-arm T=0 repeats flipped on about 27% of items, so single-question attributions in the "noise" row
are soft. The "missing seed" attributions are the ones where a's notes quote or visibly execute the specific advice
and b commits exactly the error that advice targets.

### 1.3 Was the seed text actually used by a?

Yes, broadly and visibly.

| signal (all 50 episodes) | a | b | c |
|---|---|---|---|
| reasoning notes cite "AGENTS" / "instructions say" | 22 | 3 | 23 |
| notes mention "latest printing/issue" | 43 | 20 | 43 |
| episodes that ran `printings` at least once | 42 | 31 | 40 |
| mean `printings` commands per non-loop episode | 2.5 | 1.5 | 2.2 |
| mean `calc` commands per non-loop episode | 1.9 | 1.0 | 2.0 |

(b still knew "most up-to-date revision" from the README task conventions, which explains its 20 mentions.)

Per-question judgement from `q/*.json` (a_used_seed_advice): yes 15, unclear 3 (UID0130, UID0148, UID0182),
not assessed 1 (UID0149, a and b agree). "Would b have benefited": yes 4 (UID0007, UID0027, UID0117, UID0220),
unclear 8, no 5.

The seed advice that plausibly decided an outcome:
- identity-FAILS → cross-printing check: UID0007;
- print the source line before use: UID0117;
- choose the table whose title/headers match the wording: UID0220 (and helped in UID0028, UID0133);
- weigh alternatives against the wording / decide per column: UID0109;
- percent vs fraction: UID0237;
- match the period exactly: UID0027.

It is also fragile: c had the identical text and still skipped it in UID0007, UID0220, UID0133 (printings skipped),
and a's own loop advice did not stop 10 loop runs.

### 1.4 Gate-induced changes

From the 50 check files (`api/checks/*.json`):
- 27 episodes had at least one finding; 12 of 21 right-before-gate episodes got a finding (57%) vs 15 of 29 wrong ones
  (52%) → the gate does not discriminate.
- Answer changed after a finding in 10 episodes: wrong→right 1 (UID0245), right→wrong 2 (UID0097, UID0211),
  wrong→wrong 7.
- Offers shown in 9 episodes; 7 accepted; exactly **1** accepted offer produced a right answer (UID0245).

### 1.5 Loop notes and ledger splice

- 21 loop notes in 9 episodes (UID0225, UID0030, UID0188, UID0114, UID0183, UID0243, UID0029, UID0027, UID0037);
  a repeated command still recurred after a note in 4. Max repeat in b was still 297 (UID0188), 141 (UID0029) and
  91 (UID0114), so 3 notes (`loop_notes_max`) were not enough in hard loops.
- Accuracy on those 9: b 3 right, a 3 right (same questions) → no accuracy effect. The only visible help was UID0243
  (note moved b to submit its correct kept answer).
- Cost effect is large: total input tokens a ≈ 322M, b ≈ 128M, c ≈ 38M. a's 10 loop runs (UID0215, UID0100, UID0030,
  UID0188, UID0211, UID0165, UID0114, UID0183, UID0245, UID0037) account for most of a's bill; a's text advice
  "a command repeated with the same output will not change" did not stop repeats of 40× (UID0211), 21× (UID0245),
  137–139× (UID0030, UID0188).
- Ledger splices happened only after the 4 compactions (UID0188, UID0114, UID0183, UID0029); a had the same
  correctness on all four → no measurable effect.

### 1.6 Submit-protocol side effects (not captured by flips)

- **`--alt` solicitation.** The mech SEED says "add `--alt` when you also considered another value". This turned
  private doubt into a formal alternative, which `competing_candidates` echoed back as an Offer (UID0097, UID0122,
  UID0028, UID0211, UID0214, UID0114).
- **False authority.** The SEED says an Offer is something "the harness computed … itself, from your own calculation
  with one input replaced". For `use_alt` this is false. b's notes quote it literally ("Harness says offer alt
  computed from own calculation…", UID0211, UID0028).
- **Oracle probing.** b explicitly planned to use submit as a test of the answer key in 6 episodes (UID0122, UID0225,
  UID0220, UID0030, UID0148, UID0037).
- **Deference over its own correct doubt.** UID0007: b noticed the offered cell was on the receipts panel, then
  accepted ("latest wins … accept per instruction").
- **Asymmetric Accept/Keep cost.** Accept is a ready one-liner; Keep needs a reason. Yet Keep with a prose sentence
  dismissed the only correct `grounded` finding (UID0117). Both halves are wrong for this actor: it accepts bad offers
  and dismisses good findings.
- **Suppression bug.** After an accepted offer, `recompute_final` fired but was suppressed (`offered=true`), so an
  alt that b had computed in its head with an arithmetic slip was recorded unchecked (UID0211).
- **Run length** was not systematically shorter (non-loop mean requests a 20.5, b 19.5), but b verified less
  (printings 1.5 vs 2.5 per episode) and stopped early in some (UID0007: 13 vs 26 requests; UID0097: 9 vs 15).

---

## 2. Why c (T=1.0, 24/50) lost to a (T=0, 31/50) despite 0 loop runs

Discordant: a right / c wrong 11, c right / a wrong 4 → net −7.

| mechanism | n | questions (c vs a requests) | what changed |
|---|---|---|---|
| **less verification / early commitment** | 4 | UID0007 (10 vs 26), UID0220 (8 vs 17), UID0130 (13 vs 15), UID0133 (16 vs 27) | c took the first plausible table or raw flagged cells and skipped the check a did: identity-fail cross-check (printings 1 vs 8), broader-total search (printings 1 vs 5), sibling-table search, printings entirely ("snapshot tables are not revised") |
| **interpretation coin-flip, reached by more exploration** | 4 | UID0097 (17 vs 15), UID0122 (42 vs 34), UID0237 (17 vs 9), UID0028 (42 vs 58) | c surfaced a second reading (nominal = capital account; header covers receivables; unit phrase names the output; more precise table) and argued itself into it |
| **format / formula slip** | 2 | UID0245 (×100 framing from the first calc), UID0027 (two-digit year encoding after an unstable panel reading) | token-level choices that T=0 happened to get right |
| **search path luck** | 1 | UID0182 | a's fifth phrasing found the table; c never did |
| c wins by exploration | 3 | UID0101, UID0149, UID0211 | a second reading was right (formula definition, acronym, table choice); every v8 arm and Opus also missed UID0101 → luck |
| c wins by extraction | 1 | UID0180 | a tailed `printings` to its %g-rounded SUMMARY; c read full output |

Behaviour, all 50 episodes:
- **Run length:** c median 18.5 requests (a 20.5 overall, 16 without loops); c mean 23.5 vs a 66.1 (a inflated by
  loops). On the 36 questions where neither a nor b looped: a 19.8, c 19.4 mean requests; elapsed a 149 s, c 133 s.
  c is not uniformly shorter: it was shorter exactly on the verification-lapse losses and *longer* on the
  interpretation losses.
- **Verification depth:** episodes with any `printings`: a 42, c 40. On a's non-loop episodes c ran fewer printings
  than a in 15 and more in 9.
- **Table choices:** c chose a different source table in UID0130, UID0133, UID0182, UID0220, UID0028 (4 losses +
  1 tie-break).
- **Arithmetic:** no pure arithmetic losses; c caught its own slips in UID0097 and UID0149, made a list-length slip in
  UID0007 that it repaired.
- **Loops:** c had 0 loop runs vs a's 10 — sampling breaks the T=0 fixed point. This is the only T=1 benefit, and
  it is cheaper to get from a broker loop guard.

Reading: T=1 hurts in two opposite ways — it sometimes commits early and skips the checks the seed asks for, and it
sometimes explores ambiguous wording further than T=0 and lands on the wrong side. The pre-registered rule
correctly chose **T=0**.

---

## 3. Per-rule audit (b)

"R/W" is correctness of the submitted candidate before the finding. Official counts from `v9-p1-metrics.json`
(`per_rule`); episodes from our recount of the check files. Rules never fired: `answer_count`, `period_match`.

| rule | engine fired R/W | shown R/W | offer? | offers accepted → result | ever right? | notable episodes | verdict |
|---|---|---|---|---|---|---|---|
| `competing_candidates` | 2 / 5 | 2 / 4 (+1 hidden) | always = own `--alt` | 4 accepted → 4 wrong (2 right→wrong: UID0097, UID0211; 2 wrong→wrong: UID0122, UID0028); 2 kept (UID0214, UID0114) | **no** (0/6) | adds no evidence; invites oracle use | **delete** (and drop `--alt` from SEED) |
| `later_printing` | 0 / 1 | 0 / 1 | substitute_printing | 1 accepted → more wrong (UID0007: receipts-panel cell offered for an expenditure cell) | **no** | missed UID0133 (uncaptioned latest copy); endorsed a reclassified series (UID0220) | disable offer; rebuild lineage (table+section+row label+column); then use as harness authority |
| `flagged_cells` | 2 / 5 | 2 / 4 | only once (UID0007, hidden, same panel bug) | none | **no** | UID0117: targeted a correct cell with a wrong "row 1976" hint, pushing `grounded` to round 2; UID0148: bound by value literal to another table | remove from gate; move identity-fail cross-check into `show_table`/`printings` output |
| `grounded` | 4 / 6 | 4 / 4 (+2 hidden) | none | — | caught the true error once (UID0117), dismissed by Keep | right-episode fires: UID0077, UID0213, UID0029, UID0243 (external CPI → futile search, ~13 requests) | rewrite: check at `calc` time, exempt question-sourced external data, top priority on the answer path, Keep only with a printed line |
| `recompute_final` | 3 / 8 | 3 / 3 (+5 hidden) | none | — | fixed a real encoding slip once (UID0027, still wrong on period) | right fires UID0228, UID0044, UID0204 changed nothing; suppressed after accepted offer (UID0211); silent when no calc at all (UID0149) | keep, but as harness action (answer with the calc output) and never suppressed |
| `scale` | 0 / 2 | 0 / 2 | unscale | 2 accepted → 1 right (UID0245), 1 still wrong (UID0165) | **yes** (1/2) | would miss ×100 written inside the calc expression (c's UID0245) | keep; extend to calc expressions; make it a harness decision when the question does not ask for percent |
| `table_alternatives` | 2 / 2 | 2 / 2 | none | — | no | false positive on generic words (UID0243 "adjust"); silent when the better table never entered the ledger (UID0182, UID0180, UID0130) | remove from gate; move into `find_tables` ranking |
| `row_coverage` | 1 / 2 | 1 / 1 (+1 hidden) | none | — | no | false positive on an unused flow column (UID0122); pushes toward over-inclusion (UID0109) | delete |

Whole gate: 1 useful flip (scale), 2 harmful flips (competing_candidates), 0 useful findings from the five
provenance/table rules. Pre-registered limits: right-episode fire 57% (limit 20%), right→wrong 16.7% of fired right
(limit 5%).

Offline estimate (from the same check files, not a run): with `competing_candidates`, `row_coverage`,
`table_alternatives`, `flagged_cells` and `later_printing` removed from the gate, right-episode fires drop to
`recompute_final` (UID0228, UID0044, UID0204) + `grounded` (UID0077, UID0213, UID0029, UID0243) = 7/21; exempting
external data (the CPI cases) and turning `recompute_final` into a silent harness action would leave roughly 2–3/21.
Both right→wrong flips disappear.

---

## 4. Recommendations for the v10 harness adapter

The common thread: for this actor, **advisory text at submit time is weak** (it accepts bad offers, dismisses good
findings with a sentence, and treats the gate as an oracle), while **up-front text is used** (a quotes it) but is
skipped under sampling. Mechanisms should therefore (i) sit on top of the seed text, (ii) act where the decision is
made, and (iii) decide by themselves only where a pre-registered convention makes the right action unambiguous.

### R1. Keep the seed text in every arm; add mechanisms on top
- Evidence: 6 of 12 b losses are the exact errors the text targets (UID0007, UID0117, UID0220 strong; UID0109,
  UID0237, UID0027 moderate); a cites AGENTS in 22/50 and uses printings in 42/50 vs b 3/50 and 31/50; the gate could
  not see scope/interpretation errors at all (UID0109, UID0237, UID0220).
- Design change: DESIGN §1 "seed parity" (knowledge as text *or* as rules) confounds "remove text" with "add
  mechanism". Redefine the mech arms as `text-w0 + controls`; H1 becomes `text+mech-evolve > text-evolve`. Keep the
  16 KB budget by counting rule templates inside it.
- Test: paired arms `text-static` vs `text+mech-static` (same T=0, same questions, 2 repeats); plus the ablation
  `mech-static without seed text` (= v9 b) in one repeat to confirm the ~6-question seed effect.

### R2. Remove offer-to-accept for the model's own alternatives
- Evidence: `competing_candidates` 6 shown offers, 0 right, 2 right→wrong (UID0097, UID0211), 2 wrong→wrong
  (UID0122, UID0028); oracle probing in 6 episodes; the "harness computed that alternative itself" sentence was
  quoted as authority.
- Change: delete `competing_candidates` and the `--alt` flag from the SEED; reword any remaining Offer as "a candidate
  computed by the harness from <table/section/row label>"; make Accept and Keep equally costly (both a single
  command); never suppress `recompute_final`/`grounded` after an accepted offer.
- Test: $0 offline replay of the 50 v9 check files with the rule disabled (expected: b's two pre-gate right answers
  kept); then decision-point replay (DESIGN §3, ≈$1–3) of the 9 offer episodes with the new wording, 2 samples each,
  counting flips.

### R3. Move checks earlier: into table selection and cell reading, not submit
Each item is a tool-output change or a tool-time finding, printed when the actor looks at the cell:
- identity-FAILS rows: `show_table` itself prints the cross-printing and row-total comparison and the majority value
  (UID0007; `flagged_cells` never helped at submit);
- printing lineage keyed by table + section/panel + row label + column, and linking uncaptioned reprints by row label
  (UID0007 wrong panel, UID0133 missed chart copy, UID0220 reclassified series);
- `printings` SUMMARY prints raw values, never %g-rounded (UID0180);
- stacked-panel tables: emit period-labelled long rows and show the SIBLINGS/Y? note in every view including `--raw`
  (UID0027, UID0028, UID0117 two-panel PDO-1);
- `find_tables`: cap rows per group under `--all`, index row labels, show section headings for same-title tables
  (UID0180 FD-1 crowding, UID0182 "payable in Canadian dollars", UID0211 two same-title tables);
- same-concept cross-table disagreement in the same issue (UID0130);
- `grounded` at `calc` time: calc annotates any literal not seen in tool output, before an answer exists (UID0117;
  exempt question-sourced external series such as CPI-U, UID0243).
- Test: these are L1/L0 changes; if they go to all arms they are neutral for H1 and should first be validated by
  tool unit tests on the cited tables and a $0 re-read of the cited episodes. If kept as a mech-only feature
  ("tool-time checks"), run `text-static` vs `text+toolchecks` as its own arm pair and count discordant questions
  in the families above.

### R4. Give the harness decision authority where pre-registered task conventions apply
- Conventions (README, all arms): "maximum precision unless specified"; "most up-to-date revision unless the question
  pins a date". Where the ledger proves the convention applies, the harness substitutes and records, instead of
  offering:
  - latest printing of the *same lineage* (R3 keys) when no date is pinned — only after lineage is fixed; the one v9
    offer of this kind was wrong because of the panel bug (UID0007);
  - answer = 100× a calc output and the question does not ask for a percent → unscale (`scale` was 1 right, 1 not
    enough, 0 wrong; UID0245, UID0165);
  - answer is a hand-typed or hand-rounded version of a calc output → answer with the calc output rounded as asked
    (`recompute_final`; UID0027, UID0211's unchecked mental alt);
  - `%g`/truncated display used as input → replace by the full printed value (UID0180).
- Everything else (interpretation, scope, table choice) stays with the model, as up-front text, never as an Offer.
- Test: offline counterfactual on all v9 episodes (a, b, c) — apply each authority rule to the recorded ledger and
  count right→wrong vs wrong→right (grading by the controller, no answer values leaked to the actor); admit a rule
  only if right→wrong = 0 on these 150 episodes. Then arm `text+authority` vs `text-static`.

### R5. Gate hygiene for whatever stays at submit
- Enabled at submit: `answer_count`, rewritten `grounded`, and the R4 authority rules; disable
  `competing_candidates`, `row_coverage`, `table_alternatives`, `flagged_cells`, `later_printing` as submit findings.
- Rank by measured precision; show all findings of a round (≤4 lines) rather than one, so a low-value finding cannot
  hide the real one (UID0117).
- Keep for `grounded` only with a printed line containing the value.
- Test: the pre-registered limits (right-fire ≤20%, right→wrong ≤5% of fired right) checked first on the v9 replay
  (estimate above: ≈2–3/21), then in the pilot.

### R6. Loop guard at temperature 0 in every arm
- Evidence: a (T=0, text loop advice) 10 loop runs, ≈322M input tokens, 1 timeout (UID0114); c (T=1) 0 loops but −7
  accuracy; b's broker notes cut tokens to ≈128M but hard loops continued after 3 notes (max repeat 297 in UID0188).
- Change: make the loop guard an L0 environment feature for all arms (neutral for H1), with escalation: notes 1–2 as
  now, then the broker refuses the identical command and returns "blocked: identical output already shown" plus the
  current candidates; optional ablation: one-request temperature bump (e.g. T=0.7) only on the request after a
  detected loop, T=0 otherwise.
- Test: on the loop-prone set (a's 10 loop questions + v8 loop items), arms `T=0 no guard`, `T=0 guard`,
  `T=0 guard + bump`: loop runs, input tokens, accuracy. Keep T=0 as the base (pre-registered rule already decided).

### R7. Interpretation and scope knowledge as text, not as gate rules
- Unfixed families: row membership under a group header (UID0122), unit phrase vs named operator (UID0237),
  component vs grand total (UID0243) vs broadest total (UID0220), acronym expansion (UID0149), formula definition
  (UID0101).
- These are dev-set observations; they should enter only through the text/mech learners with paired re-run
  verification (DESIGN §3), not be hand-added to the seed, or they bias the H1 comparison.

### Next pilot (proposal)

All arms T=0, v10 tools (R3) + loop guard (R6) in all arms, same 50 questions, 2 repeats each (block-paired order):

| arm | seed text | tool-time checks | submit gate / authority |
|---|---|---|---|
| P0 `text-static-v10` | ✓ | – | – |
| P1 `text+authority` | ✓ | – | R4 authority rules + `answer_count` + rewritten `grounded` |
| P2 `text+toolchecks+authority` | ✓ | ✓ | as P1 |
| P3 `mech-no-text` (1 repeat) | minimal submit note | ✓ | as P1 |

Go/no-go: P2 ≥ P0 on paired discordant counts across both repeats; harness-caused right→wrong ≤ 1 per 50; right-
episode finding rate ≤ 20%; P0 vs P3 estimates the seed-text effect under mechanisms. Before any paid call, run the
$0 offline replays of R2, R4 and R5 on the v9 records.
