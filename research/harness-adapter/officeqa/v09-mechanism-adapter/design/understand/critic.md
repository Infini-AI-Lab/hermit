# Completeness critique of the six v9 "understand" reports

Date: 2026-09-28. Inputs: `acting.md`, `broker.md`, `campaign.md`, `compaction.md`, `tables.md`, `replay.md`,
`../../officeqa-divergence-analysis/FINDINGS.md`, the v8 code in `officeqa-pro133-v8-plumbing/` (called `P/` below),
and the three v8 run roots. No reference answers, graders, `prepared/private/`, `evidence/*/reference_answer.json`,
`private-evaluation/` or credentials were opened. The learner tool-result files were read only for `name` and `ok`
flags. Correctness came only from the 0/1 flags in `officeqa-divergence-analysis/index.json`. My check scripts are in
`critic_checks/` (`spot.py`, `lat.py`, `tooluse.py`). Other one-off checks are listed in §6.

## 中文摘要

- **大方向可靠。** 我抽查了 17 条最关键的数字/代码结论，其中 15 条完全复现，1 条是报告错误（replay.md 写的压缩阈值是 160K），1 条来自 FINDINGS 的说法被推翻（「3 个 run 列出了兄弟目录」，实际 0 个）。
- **报告之间有 4 处实质矛盾，必须先拍板：**
  1. 压缩后账本怎么注入：compaction.md 主张改写压缩响应的 SSE，broker.md 明确反对，主张在摘要后面插入固定不动的 splice（拼接项）；
  2. 打转检测的规则：broker.md 的"连续 3 次"只抓到 36 个 run，其他报告的"同一命令第 3 次"抓到 76 个；
  3. 提交闸门的实现：acting.md 主张去掉 `--ephemeral` 再用 `codex exec resume`，broker.md 主张在 broker 里重新提问；
  4. 账本的可信通道：写工作区文件，还是 POST 到 broker。而 POST 一样能被 `curl` 伪造。
- **新发现 1：** v8 的 prompt 组从第 36 题起学到一条环境绕行经验（`L-python-c-not-heredoc`），heredoc 失败从 34/35 降到 26/98。L0 把环境修好以后，这部分"学习收益"对所有组都消失了。所以 v8 的 prompt ≈ none 不能当 v9 的先验。
- **新发现 2：** Codex 自带的模型目录里 9/9 个条目的输出截断都是 `tokens:10000`（约 40 KB），而 v8 实际截断在约 10 KB，标记写的是 "chars"。如果照 compaction.md 的建议用 `model_catalog_json` 声明 GLM 元数据，很可能顺带把工具输出上限放大约 4 倍，改变所有组的上下文增长和压缩时机。
- **阻塞设计的空白：**
  - 每组到底拿哪几层（尤其 L1 工具给不给 none/prompt）；
  - 进化器的输入输出、代码在哪执行、准入数据从哪来（manifest 禁止用往期实验；本轮早期只有 5–10 个带标签 episode）；
  - 账本 schema 和"模型不用账本怎么办"；
  - L4 除了 EMPTY/COUNT，没有任何经过验证的内容检查；
  - CPI 等外部数据给不给；
  - 版本决胜规则算不算 OfficeQA 专属知识；
  - 统计计划：重复次数、主指标、issue-47 怎么算；
  - 4 组如何同步调度。
- **公平性最大风险：** 71/133 题（恰好是所有组间有分歧的题）已经在带标签的情况下被逐题分析过，表格工具的 26 个测试用例也来自这些题。mech-static 其实已经"在测试集上被人工进化过"，而 prompt 组的 W0 是通用的。剩下 62 题全部是各组一致对或一致错的题（33 对 29 错），题内无法再留出。另外，单次 133 题配对差的 SD 约为 6 题，而最有证据的机制（打转守卫）预计只带来每组约 +5 题，每组跑 2 次时检出 10 题差距的功效也只有约 0.66。

## Terms used below

- **Splice**: a broker-owned message inserted at a fixed place in the request history and re-sent on every later request.
- **SSE** (Server-Sent Events): the streaming response format that Codex reads.
- **Loop guard**: the L3 rule that notices a repeated command and injects a nudge.
- **Discordant pair**: a question that one arm got right and the other got wrong. The number of discordant pairs sets
  the noise of a paired comparison.
- **Power**: the probability that an experiment detects a real difference of a given size.

---

## 0. Verdict

| Area | State | Blocking? |
|---|---|---|
| Facts about v8 (environment, broker, compaction, timeouts, loops, corpus) | Reproduced in 15/17 spot checks. One report error and one FINDINGS error found (§2). | No |
| Cross-report design recommendations | 4 real conflicts (C1–C4) and 5 minor ones (§1) | **Yes**: C1–C4 each change the code layout |
| Arm definitions and evolver spec | Not written by anyone (G1, G2) | **Yes** |
| L4 checker content | Only EMPTY and COUNT are validated. Everything else depends on ledger data that v8 does not have (G4). | Yes, for what "mech-static" means |
| Statistical and fairness plan | Not written. Test-set exposure is large (R1). Power is low for realistic effects (R6). | **Yes** |

---

## 1. Contradictions between reports

### C1. How the ledger is re-injected after compaction (compaction.md vs broker.md)

- **compaction.md §5 H1 ("primary")**: rewrite the compaction *response*. Append the rendered ledger to the summary's
  `output_text` and re-serialize the SSE events (`output_text.delta/done`, `output_item.done`, `response.completed`)
  before `broker.py:413`.
- **broker.md §3d**: rejects exactly this ("Rejected alternative: rewriting the summary text inside the compaction
  response… breaking the forward-unchanged property"). It proposes a *sticky splice*: a `[harness:ledger]` user item
  anchored on the summary item's `id`, frozen at insertion, re-sent until the next compaction. Its design invariant (iv)
  is "forward only real provider bytes" (§6).
- **compaction.md H2 (fallback)**: a *trailing* ledger message, re-rendered on every request. This conflicts with both.
  The message moves every turn, so the model sees a history that does not match any single request.
- **Recommendation.** Use broker.md's splice, anchored after the summary and re-created after each compaction.
  - It keeps provider bytes unmodified. Codex's `events.jsonl` then still equals what the provider produced, and the
    audit and replay stay simple.
  - H1 would also put harness text inside a "model output" that the next compaction and the learner trajectory treat as
    GLM's own words. That misattributes behavior.
  - Keep compaction.md's H0 (show the ledger to the summarizer) as optional. Both reports agree it is lossy.

### C2. Loop-guard rule: consecutive streak vs any 3rd issue (broker.md vs replay.md / compaction.md)

- **broker.md** implements `tail_streak`: "the last ≥3 `function_call`s have identical `(name, arguments, normalized
  output)`". It fires in 36 runs; the golden numbers are 14/9/13 runs (§3a, §6.2, test 22).
- **replay.md §6 and compaction.md §2** recommend "the same command issued a 3rd time anywhere in the run". It fires in
  76 runs (71/72 of the compacted runs).
- **I re-computed both** (`critic_checks/spot.py`, plus `replay/results.jsonl`):

  | Rule | none | prompt | cards | right among them |
  |---|---|---|---|---|
  | consecutive ≥3 | 14 | 9 | 13 | 3 / 1 / 3 |
  | any 3rd issue | 22 | 25 | 29 | 4 / 3 / 5 |

- The consecutive rule misses 40 loop runs, including alternating A,B,A,B cycles. It catches 11 of the 13 timeouts;
  the any-3rd rule catches 13 of 13.
- broker.md's rule also sees only the history since the last compaction, unless its proposed command log is built.
- **Recommendation.** Implement the any-3rd rule over a broker-side command log that survives compaction. Use 22/25/29
  as the replay goldens, and keep 14/9/13 as a secondary golden.

### C3. How the L4 commit gate reaches the model (acting.md vs broker.md)

- **acting.md §4** prefers (a): drop `--ephemeral` (`P/acting.py:323`), and after a completed turn run
  `codex exec resume <thread_id>` with the checker feedback. It rejects "broker rewrite" because that would fabricate
  model output.
- **broker.md §3c/§6** prefers an in-broker re-query. The broker appends `echo(draft) + feedback`, asks the provider
  again, and forwards the *revision's own* wire bytes. It does not fabricate anything.
- These are two different designs, and neither report weighs them against each other.

| | Resume (acting.md) | Broker re-query (broker.md) |
|---|---|---|
| Codex records (`events.jsonl`, `final.md`) | include the draft and the feedback | never see the draft or the feedback (broker.md risk 4) |
| Learner and trajectory pipeline | unchanged | needs a merge with `controller.jsonl` |
| Codex acceptance | new process; resume semantics are unverified | Codex receives a response to input it never sent. The only proof would be broker.md test 23, which has not been run |
| Prompt cache | resume re-sends the history (cache probably warm) | preserved by construction |
| Wall clock | new process start; the remaining budget must be passed on | inside the same request |

- **Needed**: one decision, then the corresponding live-Codex smoke test (broker.md test 23, or a resume smoke). No
  report has run either.

### C4. Ledger trust channel (acting.md vs compaction.md vs broker.md)

- **acting.md §3.3** wants `calc`/`ledger` to POST to a new broker route (`/v1/ledger`), because "a ledger kept [in the
  workspace] can be forged by a plain `echo`".
- **compaction.md H1** reads `workspace/.ledger/ledger.jsonl`, which is actor-writable. **broker.md §3d** allows either.
- **acting.md's premise is only half right.** The Seatbelt rule
  `(allow network-outbound (remote tcp "localhost:<port>"))` (`P/acting.py:247`) covers the whole actor process tree,
  and `/usr/bin` is readable. The model can therefore `curl` the same route, so a POST route is no harder to forge than
  a file.
- Genuine unforgeability would need controller-side execution. There is a precedent: `/v1/local-mwm` runs code in a
  controller-owned sandbox, not in the broker process (`P/broker.py:228-242`, `campaign.py:123-125`).
- **What v8 shows**: in all three arms, 0 actor commands touched `127.0.0.1`, `localhost`, `curl`, `urllib` or
  `requests`. GLM is not adversarial.
- **Recommendation.** Decide explicitly that the ledger is honest bookkeeping, not a security boundary, or else build
  controller-side `calc`. Do not advertise the broker port in README.

### C5–C9. Minor inconsistencies

- **C5 (report error).** replay.md §1 says the auto-compact limit is "160,000 tokens, `acting.py:284`". That is the base
  profile only. `P/acting.py:293-295` overrides it under FULL to 1,048,576 / 800,000, and the effective trigger is
  244,800 (compaction.md §4). This does not change replay.md's numbers, but it should be corrected.
- **C6 (FINDINGS error).** FINDINGS §2.5 says 3 runs *listed* other episodes' directories. acting.md says those runs
  only attempted it and were denied.
  - I searched every `command_execution.aggregated_output` in all 399 runs for any other episode's
    `officeqa-pro133-*-v8-work/runs/<arm>/NNN` path and found **0 hits**. acting.md is right.
- **C7. The request layout differs between reports.**
  - acting.md §1.3 lists 5 input items (two developer messages, a separate `<environment_context>`).
  - `none/072/api/001/request.json` actually has **3** items:
    1. one developer message holding both the skills and the permissions text;
    2. one user message with AGENTS.md plus `<environment_context>`;
    3. the task.
  - This matters for splice anchors and for checking that `include_permissions_instructions=false` removes only the
    permissions part.
- **C8. Output limits and page sizes.**
  - acting.md suggests `tool_output_token_limit: <N>` or `model_catalog_json`. compaction.md says do not raise the tool
    output limit. tables.md pages at 9 KB, and compaction.md at ≤8,000 B.
  - See my new finding N2 (§2): a catalog entry probably *does* raise the limit unless `truncation_policy` is set
    explicitly.
  - Pick one page budget (≤8,000 B leaves room for Codex's header).
- **C9. "Silent" truncation.** FINDINGS and the v9 plan say "no silent output truncation". acting.md shows the
  truncation is marked (a header plus `…N chars truncated…`). The real problem is lost middle rows. Reword the L0 item
  as "tools paginate; never rely on Codex truncation".
- **Also.** compaction.md's "fixed prefix ≤8K tokens for AGENTS.md + L5 notes" must mean *added on top of* the ~7.3K
  tokens of Codex instructions plus tools, which the none arm already spends on request 001.

---

## 2. Spot checks of load-bearing claims

| # | Claim (report) | How I checked | Result |
|---|---|---|---|
| 1 | Any unknown architecture silently becomes `mwm` (campaign.md) | `P/experiment.py:12` | **Confirmed** |
| 2 | FULL sets multi-agent and a 1M/800K window for **all** arms, including none (acting.md) | `P/acting.py:293-295`; the none root's profile is `full_cards_unbounded` (`state.json`) | **Confirmed** |
| 3 | No `TMPPREFIX` in the actor environment; PATH puts the venv and `cua_node` first (acting.md) | `P/acting.py:166-174` | **Confirmed** |
| 4 | Every actor command ran as `/bin/zsh -lc` (acting.md: 21,507) | all `item.started` in 399 `events.jsonl` | **Confirmed**: 21,508 of 21,508. The off-by-one is immaterial. |
| 5 | Tool-less requests are compactions, 35/38/46 (broker.md, compaction.md) | `grep -F '"tools": []'` over every `request.json` | **Confirmed**: 35/38/46; totals 7,041/6,354/8,691 requests |
| 6 | Compaction triggers at about 244.8K, never above 247,847 (compaction.md) | max per-attempt `input_tokens` over all `transport.json` | **Confirmed**: max 247,847; compaction attempts 241,474–247,847 |
| 7 | Compacted runs were right 12/72 vs 169/327 (compaction.md) | compaction flag × `index.json` | **Confirmed**: none 3/21 vs 59/112, prompt 4/22 vs 60/111, cards 5/29 vs 50/104 |
| 8 | 13 timeouts (4/5/4), all empty; 12 compacted (acting.md, compaction.md) | `summary.json` status + compaction flag | **Confirmed** |
| 9 | Timeout gives an empty answer (acting.md, campaign.md) | `P/campaign.py:133-134` | **Confirmed** |
| 10 | Broker forces `temperature=0`; lock blocks in FULL (broker.md) | `P/broker.py:90`, `:246` | **Confirmed** |
| 11 | A user message after `function_call_output` is accepted (broker.md §3b) | `none/054/api/136/request.json`: last item is user `<environment_context>`, status `completed` | **Confirmed** |
| 12 | Loop counts 36 (streak) and 76 (3rd issue) | recomputed from events (C2) | **Confirmed** |
| 13 | Prompt learner's native file tools failed 100% (campaign.md) | `ok` flags of 2,969 learner tool results | **Confirmed**: read_file 0/65, write_file 0/28, list_files 0/28, validate 0/38, read_path 0/25, list_path 0/5, all `PermissionError … harnessadapter/__init__.py`; bash 713/713, run_command 1,376/1,376 |
| 14 | `full_audit` requires learner `reasoning_effort is None`; cards admission file is hard-coded True (campaign.md) | `P/full_audit.py:78`, `P/cards_learning.py:637-641` | **Confirmed** |
| 15 | Opus and v8 corpora are identical (tables.md checked 3 files) | sha256 of all 697 files in 4 locations | **Confirmed** for all 697 (aggregate digest `1123c75ca5d01e27` everywhere) |
| 16 | Every table is a pandas pipe table with a separator and no ragged rows (tables.md; 11 issues sampled) | corpus-wide scan of pipe blocks | **Essentially confirmed**: 95,054 blocks, 1 without a separator (`treasury_bulletin_1959_04.txt:295`), 4 ragged |
| 17 | Auto-compact 160,000 (replay.md §1) | `P/acting.py:284` vs `:293-295` + data | **Wrong** (C5) |
| — | FINDINGS: "3 runs listed sibling directories" | path search in all outputs | **Wrong** (C6) |

### New findings that no report mentions

- **N1. v8's prompt arm partly learned environment workarounds, and L0 erases that target.**
  - Runs with ≥1 heredoc failure (`behavior.json`): none 131/133, **prompt 60/133**, cards 133/133. FINDINGS §2.5
    reports only none and cards.
  - The prompt learner's lesson `L-python-c-not-heredoc` is first served at Q36. Its text tells the model that heredocs
    fail and to use `python -c`.
  - Heredoc-failure runs before and after Q36:

    | arm | before Q36 | Q36 and later |
    |---|---|---|
    | none | 35/35 | 96/98 |
    | prompt | 34/35 | **26/98** |
    | cards | 35/35 | 98/98 |

  - Consequences:
    1. v8's prompt ≈ none parity (64 vs 62) includes environment adaptation.
    2. Under L0 the v9 prompt-evolve arm has no such easy target, so it is a different arm and v8 is not a prior for it.
    3. An evolver that can edit tools would also have "fixed" this. L0 removes one of the evolvers' cheapest wins for
       every arm. That is fair, but it should be stated.
- **N2. Declaring model metadata may quadruple the visible tool output.**
  - The Codex 0.155.0-alpha.9.2 binary bundles 9 model entries. All 9 have
    `"truncation_policy": {"mode": "tokens", "limit": 10000}` (from `strings`).
  - v8's truncation markers read `…N chars truncated…`. In the last requests of 40 none runs, 155 outputs were
    truncated, with a maximum of 10,216 bytes. This points to a byte/character fallback of about 10,000.
  - compaction.md's preferred fix ("give Codex real metadata… through `model_catalog_json`") would then likely lift the
    per-output cap from ~10 KB to ~10K tokens (~40 KB). That changes context growth, compaction timing and cost for
    every arm.
  - The v9 L0 must set `truncation_policy` explicitly, and the smoke test must assert the truncation size, not only the
    compaction point.
  - Unverified: whether a bytes mode exists in the catalog schema.
- **N3. The time budget does not bind except in loops.** From `critic_checks/lat.py`:
  - Per-call latency: p50 4.6 s, p90 10.0 s, p99 31.9 s, max 1,460 s.
  - Completed runs: elapsed p50 102 s, p90 777 s, p99 2,779 s. **0 of 386** completed runs ended within the last 300 s.
  - So a gate round costs about 5–10 s, and `gate_min_remaining_s=240` would almost never skip a gate.
  - "Nudge at 50%/80% of 3,600 s" would fire only on long, looping runs. It is redundant with the loop guard, so drop it
    or re-anchor it to command counts.
- **N4. GLM adopts documented workspace tools.** From `critic_checks/tooluse.py`:
  - `officeqa.py` (the README-documented helper) was used in 130/97/128 of 133 runs (none/prompt/cards).
  - `rg` was attempted in 133/121/133 runs even though it always failed.
  - L1 CLIs documented in README will likely be used. Text procedures were followed completely only 2/32 times
    (FINDINGS §2.2).
  - So a ledger that depends on the model *choosing* to call `ledger` is the weak point, unlike tools that record
    automatically (G3).

---

## 3. Gaps that block writing the v9 design

- **G1. Which layers does each arm get?**
  - FINDINGS §4 puts L0 in all arms. It never says whether the L1 tools (and the table index) go to none and
    prompt-evolve.
  - acting.md puts shims and L1 CLIs in one kit (`.officeqa/`). campaign.md §3.1 #5 says "applies to every arm that
    should get them".
  - Also undecided for prompt-evolve:
    - fix the broken learner file tools (spot check 13)?
    - learner reasoning effort: provider default vs `high` (`P/full_audit.py:78` enforces `None`)?
    - keep the reference shelf?
    - cap `ADAPTER.md`? It reached 231,899 B, about 84K tokens, roughly ⅓ of the 244.8K effective window, and it is
      re-sent after every compaction (compaction.md §1, §3).
  - Each choice changes what "prompt-evolve (v8 同款)" means.
- **G2. The evolver itself is unspecified.** Nobody has written:
  - **Inputs.** The prompt learner sees this block's references only (`postmortem.py:224-226`); the cards learner sees
    all released references (`cards_learning.py:217-246`). Which does mech-evolve get? The replay admission test needs
    all released labels.
  - **What it may edit** (checkers, tool heuristics, thresholds, notes), and **size and budget caps**.
  - **Where learner-authored code runs.** broker.md risk 7 says checkers must be pure and model code must never run in
    the broker process. FINDINGS §4 plans learner-written Python checkers run at the commit gate. That needs a sandboxed,
    time-limited checker runner, and the same runner must be used online and in replay.
  - **Admission data.**
    - The manifest forbids "baseline-arm trajectories" and "earlier experiments" (`P/data.py:134`), so replaying over v8
      episodes breaks the declared protocol unless PROTOCOL.md changes (campaign.md §3.4).
    - v8 episodes have no ledger, so content checkers cannot be replayed on them anyway.
    - Within the campaign, block b has only 5(b−1) labelled episodes.
  - **Admission statistics.** replay.md's CIs are ±10–15 points even at n=399. The criterion "hits the failure it fixes,
    rarely fires on right episodes" is not testable at n=5–30.
  - **Detection is not effect.** Replay measures detection only (replay.md §12). A checker whose feedback makes GLM
    change right answers would still pass. cards used paired re-runs for exactly this; the mech evolver drops them
    without a substitute.
- **G3. Ledger schema and adoption.** Still open:
  - Who writes entries: the model voluntarily, or `calc` / `show_table --long` / `printings` automatically?
  - What a "candidate" is.
  - How the gate and the timeout fallback find "the best candidate" when the model never wrote one.
  - How ledger files reach the learner: `assemble_evidence` copies only an events-derived trajectory
    (`P/campaign.py:231-244`), with *full* `aggregated_output`, not the ~10 KB the model saw. So today the evolver's view
    differs from the model's view.
  - Given N4, make recording automatic: every calc and every displayed cell gets an id, and the final answer must cite
    ids.
- **G4. L4 has no validated content.**
  - replay.md shows that only EMPTY (a timeout, which the gate never sees) and COUNT (2/399) separate right from wrong.
    Format, provenance and doubt checks do not.
  - The content checkers FINDINGS lists (period label, vintage conflict, parts = total) cannot be scored on v8.
  - So mech-static's L4 is close to a no-op on current evidence. Either run a small paid pilot to measure checker
    precision on v9 ledgers before the full campaign, or define mech-static without L4 claims.
- **G5. Unverified Codex behavior that the design depends on.** Nobody has tested these with a live Codex:
  - that `allow_login_shell`, `include_permissions_instructions`, `model_catalog_json` and `tool_output_token_limit` do
    what the reports assume (the key strings exist in the binary; semantics are unknown);
  - that Codex starts inside the narrowed profile without `cua_node`, the venv and `harnessadapter/` (acting.md §3.2);
  - C3's resume path or broker re-query path;
  - N2's truncation side effect;
  - whether the workspace `.codex/config.toml` is really ignored (acting.md asserts it; not checked).
- **G6. External reference data.**
  - 11/103 analysed failures are EXTERNAL_KNOWLEDGE, almost all CPI (FINDINGS §1). The optional `ref` tool is
    undecided.
  - Giving CPI data to mech arms only would be domain knowledge presented as a mechanism. The choice must be all arms or
    none.
- **G7. The vintage tie-break rule is key-informed domain knowledge.**
  - FINDINGS §2.6 wants "a fixed tie-break order". The per-question analyses that motivate it reason from which printing
    matches the key (e.g. `per-question/081.json`: "differs slightly from the latest-revision reference").
  - Any default rule in mech-static's checkers or notes is OfficeQA-specific and derived from the keys, which conflicts
    with FINDINGS §4 "v9 initial content must not contain OfficeQA-specific knowledge".
  - Decide: surface the conflict and let the model choose (a generic mechanism), or declare the rule.
- **G8. Statistical plan.** Missing:
  - primary metric (strict tolerance 0, as graded in `P/data.py:304-307`, vs 1%);
  - issue-47 handling (18 questions; v8 none 8, prompt 6, cards 6 right);
  - the number of repeats, the handling of 4 arms with multiple comparisons, and a pre-registered primary contrast.
  - Numbers in §4 R6.
- **G9. Scheduling four arms.**
  - `LOCKSTEP` supports exactly 2 arms (`P/experiment.py:30`).
  - v8's single-arm roots drifted: they finished after 13.7 h, 22.8 h and 27.5 h (`state.json`), so the same question
    ran up to ~14 h apart under different provider conditions.
  - Four arms mean four roots, with even more drift, unless a question-major interleaved scheduler is written.
- **G10. Cost and critical path.**
  - v8 formal totals were $25.11 / $40.06 / $49.95 (lower bounds; campaign.md §5). 4 arms × 2 repeats is about
    $250–350 plus the mech learner and the gate re-queries.
  - `cost.py` does not see new call sites (campaign.md §5).
  - Learning was 33–36% of wall clock. Nobody has estimated the mech learner.
- **G11. Held-out validity** (see R1). FINDINGS §4 names DocVQA/Spreadsheet or non-Pro OfficeQA. Nobody has scoped them,
  and non-Pro answers are public (a user decision).

---

## 4. Risks to the fairness and validity of none vs prompt-evolve vs mech-static vs mech-evolve

- **R1. mech-static is already tuned on the test set.**
  - The per-question (54) and reverse (17) analyses cover **71/133** questions, with correctness labels and Opus
    reference traces.
  - tables.md's 21 structural cases come from 26 of these questions (Q8…Q130). The `printings` tool is validated as
    "same four issues Opus found".
  - replay.md's format parser is "heuristic regex written after reading the 133 question texts" (§12).
  - The 62 questions nobody inspected are exactly the ones where all arms agree (33 all right, 29 all wrong). They carry
    no information for a comparison, so no held-out subset exists inside the 133.
  - prompt-evolve's W0 is generic, so none vs mech-static measures "hand-evolved on the test discordant set" vs nothing.
  - **Mitigation.**
    - Label mech-static as developer-tuned.
    - Audit `otables.py` for question-specific constants. I found none; only examples in comments, e.g.
      `otables.py:8-9, 88, 550, 575`.
    - Report results on an external held-out set before claiming a mechanism advantage.
- **R2. Layers are confounded.**
  - mech-evolve vs prompt-evolve differs in L1 tools + table index + L2 + L3 + L4 + evolver.
  - "Mechanism > text" is claimed from that one contrast. If L1 goes only to mech arms, add a `none+L1` or
    `prompt-evolve+L1` cell, or claim only "package > text".
- **R3. Learners see different information.**
  - They differ in what they see (G2), in reasoning effort (provider default vs actor `high`; `P/client.py:43-45`), in
    tool breakage (spot check 13), and in context (prompt `ADAPTER.md` uncapped vs cards 16 KB).
  - Equalize these, or declare them.
- **R4. Compute is unequal.**
  - Gate re-queries, sticky notes and ledger splices add upstream tokens that Codex never reports (broker.md §3c).
  - The arms are compared at equal wall clock (3,600 s), not equal tokens. Report cost per arm from broker
    `summary.json`, not from Codex usage.
- **R5. The timeout fallback gives only the mech arms an extra chance.**
  - "Submit best ledger candidate at timeout" turns empty answers into graded ones in mech arms only (13/399 empties in
    v8, all loop runs).
  - Declare it, and report results both with and without it. broker.md already suggests a separate `fallback_answer`
    field.
- **R6. Low power for realistic effects.**
  - v8 pairwise discordance was none–prompt 34, none–cards 37, prompt–cards 37. The SD of a single-run paired difference
    is √34–37 ≈ 6 questions, consistent with FINDINGS §4.
  - Expected size of the best-supported mechanism: on questions where some arms looped and others did not, the looping
    arms were right 12/61 and the non-looping arms 25/62. With about 25 loop runs per arm, even a perfect loop guard is
    worth roughly **+5 questions per arm-run**, about 1 SD.
  - With 2 independent runs per arm: SD ≈ 4.2, power ≈ 0.66 for a 10-question gap and ≈ 0.22 for 5 (two-sided α 0.05).
    With 3 runs: ≈ 0.82 and ≈ 0.30.
  - Evolve arms are noisier still: a repeat is a whole campaign with a different learned adapter.
  - Pre-register one primary contrast, and budget ≥3 repeats for it.
- **R7. Provider and time drift between roots** (G9). v8 roots ran up to ~14 h apart. Temperature 0 still shows about
  27% flips, so the provider is nondeterministic. Interleave the arms per question, or at least start them together and
  record the drift.
- **R8. The environment may change during the campaign.**
  - The actor python (`~/.cache/codex-runtimes/...`) and the ChatGPT.app `rg` are app-managed (acting.md §6).
  - A mid-campaign app update would stop some roots at `verify_freeze` and not others.
  - Copy both into the campaign root, and hash them into `freeze.json` for all arms.
- **R9. The protocol text no longer matches.**
  - `broker.py:1-5` says "an HTTP transport, not an agent loop".
  - PROTOCOL.md describes only defect re-sends.
  - The manifest forbids replaying earlier experiments (`P/data.py:134`).
  - L2–L4 and v8 replay admission need these texts changed openly before the run, or the audit will pass on a protocol
    the code no longer follows.
- **R10. Checker overrides and model-visible harness text.**
  - FINDINGS §4 lets the model "override with a reason". Offline replay cannot evaluate overrides.
  - Loop and gate notes make the mech arms' prompts differ mid-run, so the analysis tools that read only `events.jsonl`
    (`build_digests.py:41`, `behavior_stats.py:21`) will misattribute mech-arm behavior unless they merge
    `controller.jsonl` (broker.md risk 4).

---

## 5. Decisions the user or designer must make, in dependency order

1. **Arm matrix**: which arms get L1 tools, a CPI `ref` tool, and a vintage tie-break rule (G1, G6, G7, R2).
2. **Held-out strategy and statistics**: an external set? Repeats (≥3 for the primary contrast), primary metric,
   issue-47 handling, one pre-registered contrast (G8, G11, R1, R6).
3. **L4 transport**: resume or broker re-query, then the matching live smoke (C3, G5).
4. **Ledger**: automatic recording by tools, splice after the summary (C1), trust channel as bookkeeping or
   controller-executed (C4), evidence copying for the learner (G3).
5. **Loop rule**: any-3rd over a cross-compaction log (C2).
6. **L0 Codex config**: explicit `truncation_policy`, and verify the new keys (N2, G5). Record prompt-arm changes versus
   v8 (N1, G1).
7. **Evolver spec**: inputs, editable surface, sandboxed checker runner, admission rule that tolerates small n, and
   either a paired-rerun substitute or an explicit "detection only" limitation (G2).
8. **Scheduler and cost**: a 4-arm interleaved scheduler or declared drift; `cost.py` groups; budget sign-off (G9, G10).

---

## 6. Reproduce

- `python3 critic_checks/spot.py`: compaction, timeout, loop rules, total commands, sibling-path search (~3 min).
- `python3 critic_checks/lat.py`: call latency and elapsed distribution.
- `python3 critic_checks/tooluse.py`: helper, `rg` and python adoption per arm.
- One-off checks run inline in this session:
  - `grep -F '"tools": []'` counts;
  - corpus sha256 over 4 locations;
  - the pipe-table scan;
  - `strings` over `frameworks-v8/codex-0.155.0-alpha.9.2/codex` for config keys and `truncation_policy`;
  - learner tool `ok` flags;
  - heredoc failures before and after Q36 from `behavior.json`;
  - discordance and loop-mixed-question counts from `index.json` and `replay/results.jsonl`.
