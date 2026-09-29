# v10 pre-registration — OfficeQA Pro, first-50 development subset (written 2026-09-29; hashed after the paid calibration)

Status: written at integration, after S4 (W0 content merged, replay re-run, controls change applied) and before the
paid calibration. In this tree the code reads the file under its fixed name `PREREG.md` (a symlink to this file;
`run.py roots` materialises it as a regular `PREREG.md` in every root). `preflight.py` records its sha256 in
`preflight/readiness.json`, the campaign in `freeze.json`, and `run.py run` refuses a root whose `PREREG.md` hash
differs from the one preflight recorded. Exactly two slots are left open, both marked `[FILL AT FREEZE]` (each also
carries preflight's pending-decision marker, so `preflight.py` lists exactly these two): the calibration outcome
(§6) and the launch-time cost decision about root `e` (§1). The owner fills them after `calibrate.py` has run, then
hashes. Changes after hashing go in a dated addendum at the end.

Design: `officeqa-v10-design/DESIGN.md` (draft 2a) and `review/DESIGN-REVIEW.md`; protocol: this tree's
`PROTOCOL.md`; W0 content: `officeqa-v10-analysis/synthesis/W0-PROPOSAL.md` and `docs/w0/`; integration:
`docs/INTEGRATION-REPORT.md`.

## 中文摘要（写给初学者）

**这份文件是什么。** "预注册"（pre-registration）是在花钱跑实验*之前*把"要跑什么、怎么算分、怎么解读"全部写死并
取哈希（SHA-256，一种把文件内容压缩成 64 位十六进制指纹的算法：内容改一个字节，指纹就完全不同）。这样事后没法悄悄改
规则来迎合结果。`preflight.py` 会记录这个指纹，`run.py run` 发现根目录里的副本指纹不同就拒绝启动。

**要跑什么。** 同样的 50 道 OfficeQA Pro 开发题（子集 `pilot-first50`），5 个根目录各跑一组（arm）：
`a` 干净基座（plain）、`b` 只有文字的适配器 + 小抄学习器（prompt-evolve）、`c` 手工起点适配器 W0（文字 + 工具 +
控制；harness-static）、`d` W0 + 能改工具和加小抄的学习器（harness-evolve）、`e` W0 + 只能加小抄的学习器
（harness-evolve-notes）。温度 0、不设 top_p、每 5 题一个 block、先答后学（prequential：每题在看到参考答案之前作答）。

**怎么读结果。** 只做描述，不做假设检验：单次运行下，同一配置重跑也会有约 27% 的题翻转，所以两组相差 4 题以内算噪声，
相差 8 题及以上才说"不止是噪声"。`d` 对 `e` 是唯一的"单因素"对比（只差"能不能改工具"）；`c` 对 `a` 和 `b` 对 `d`
对 harness 一侧天然乐观，因为 W0 的工具改动就是从这 50 题里的失败设计出来的（§2 的曝光记录列出了每项改动针对哪些题：
50 题里有 45 题被至少一项改动针对，只有 5 题没有）。

**几个词。** *adapter（适配器）* = harness 之外的一切（SEED.md 文字、notes 小抄、tools 工具、controls.json 控制）；
*W0* = 手工做好的起点适配器；*revision（修订）* = 学习器提出的一包工具改动；*paired re-run（配对重跑）* = 改动只在
"本该修好的题"和"原本答对的对照题"上各重跑 2 次才采纳；*replay（回放）* = 零成本地用旧工具和新工具重跑记录过的命令
比较输出；*hard block（硬拦截）* = 模型反复敲同一条命令且输出相同时，运行时插入的一条"已经给你看过了"的提示。

## 1. What this run is

A **descriptive, exposure-matched development run** of five arms on the same 50 OfficeQA Pro questions used by the
v9 pilots (subset `pilot-first50`, sha256 `c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d`,
§5.2), one repeat, temperature 0, no top_p, blocks of 5, prequential (every question is answered before its
reference is released; the references of the 5 issue-47 items in the subset — UID0007, UID0154, UID0155, UID0213,
UID0237 — are scored but never released to any learner, so a learner sees at most 45 references).

| root | arm | adapter | learner | controller (controls.json) | gate |
|---|---|---|---|---|---|
| `v10-r1-a` | `plain` | none | — | no | off |
| `v10-r1-b` | `prompt-evolve` | prompt W0 (`SEED.md` only, 4,092 B) | `text` (scoped notes; `prompt_learning.py`) | no | off |
| `v10-r1-c` | `harness-static` | harness W0 (SEED 4,053 B + tools + controls) | — | yes | off |
| `v10-r1-d` | `harness-evolve` | harness W0 | `harness` (notes + tool revisions; `harness_learning.py`) | yes | off |
| `v10-r1-e` | `harness-evolve-notes` | harness W0 | `text` (notes only, on the harness W0) | yes | off |

Root names are neutral; the mapping root → arm lives only in each root's `.task-paths.json`, `freeze.json` and the
barrier's roots list (`v10-r1-barrier`, timeout 43,200 s). Whether root `e` runs is a launch-time budget decision:

- **Root `e`:** **runs** (decided 2026-09-29 at freeze). Cost estimate at pilot-1 rates (Fireworks GLM-5.3-flash,
  $0.15 / $0.03 cached / $0.50 per M tokens): `a` plain ≈ $10 (v8/v9-a actor spend on 50 questions, loops included),
  `c` harness-static ≈ $5–8 (hard loop block; v9-a spent 90 % of its tokens in 9 runaway runs), `b`/`d`/`e` evolve
  arms ≈ $15–30 each (scored episodes + ≤ 34 verification actor runs per block × 10 blocks, most blocks admitting
  fewer); round total ≈ $70–120. The owner set no budget cap for this round; `e` is the only one-factor control for
  "the learner may also change tools" (§2), so it runs with the other four roots behind the same barrier.

Not a hypothesis test, not a confirmatory study: the 133 Pro questions are the **development** set, and the 50 here
are the ones every W0 change was designed on (§2). Confirmation on unseen questions (non-Pro OfficeQA or
SpreadsheetBench) follows this round and is required before any claim about tool evolution.

## 2. Named contrasts and their exposure

| contrast | question it answers | exposure |
|---|---|---|
| `d` vs `e` | does letting the learner change tools add anything over notes alone? | **one factor**: same W0, tools, controls, SEED, USAGE.md; both learners see the same evidence (`learner_common.tools_evidence_section` is built for every harness adapter and is byte-identical in both prompts), read the same adapter files (`read_adapter_file` / `search_adapter_file` over `tools/*.py`, `USAGE.md`, `manifest.json`, `controls.json`); only `stage_tool_patch`, `stage_tool_file`, `try_tool` and `propose_revision` differ (a test asserts the tool lists differ by exactly these four) |
| `d` vs `c`, `e` vs `c` | does evolution help on top of the hand-built W0? | matched (same W0) |
| `b` vs `a` | does notes-only evolution help a text-only adapter? | matched on the text side |
| `c` vs `a` | hand-built harness adapter vs the clean base harness | **optimistic for `c`**: the W0 tools and text were designed from these questions' v9 failures (exposure record below) |
| `b` vs `d` | prompt-adapter evolution vs harness-adapter evolution (system contrast: adapter architecture, W0, learner evidence and learner all differ) | **optimistic for `d`**: every tool-side development fix lands only on the harness side; the prompt W0 carries no tools |

`prompt-evolve` is **scoped-note evolution (paired-rerun admitted)**, not GEPA/SkillOpt: its learner appends ≤ 3
triggered notes of ≤ 600 B per question and never rewrites the seed.

**Exposure record** (`packs/officeqa/pack.py: exposure_record()`, from W0-PROPOSAL §(d); `*` = issue-47 disputed
item). Every contrast is reported on all 50 and on the questions not named by any W0 change.

| change | tool | what | development items it was designed from | disputed |
|---|---|---|---|---|
| W0-1 | `find_tables` | scope title+header+row labels by default, prefix tokens (4+ letters), ranked OR fallback with 'best partial matches', a match: field per group, --title-only | UID0182, UID0029, UID0133, UID0180, UID0218, UID0237, UID0149, UID0225, UID0117, UID0211, UID0170, UID0077, UID0243, UID0097, UID0224, UID0245 | UID0237 |
| W0-2 | `find_tables` | group-first listing ([FD-1] title, issues, rows, as-of), latest 3 parts per group, --all capped at 12 per group, cut trailer, 'Table N.' title merge, [snapshot] tag | UID0243, UID0180, UID0133, UID0135, UID0094, UID0007, UID0154, UID0237, UID0149, UID0220, UID0130 | UID0007, UID0154, UID0237 |
| W0-3 | `printings` | SUMMARY keyed by (part, panel stub, column, period, as-of), --section, column-index warning, snapshot line | UID0007, UID0044, UID0170, UID0211, UID0130, UID0220, UID0188, UID0228, UID0165, UID0214, UID0218 | UID0007 |
| W0-4 | `printings` | exact SUMMARY values (Decimal-keyed groups, raw text, no %g) | UID0180, UID0135, UID0122 | — |
| W0-5 | `printings` | no-match diagnostics: printed span and last issue, --col miss with the column list, row window, periods-as-columns hint, date-keyed rows, intra-word spaces | UID0114, UID0218, UID0057, UID0097, UID0122, UID0148, UID0109, UID0180, UID0225, UID0170, UID0101 | — |
| W0-6 | `show_table` | --grep semantics and 0-row message, '\|' = OR, month-name forms, --period FY2021, --from-row past-end message | UID0170, UID0224, UID0237, UID0100, UID0112, UID0102, UID0057, UID0149, UID0214, UID0220, UID0182, UID0007, UID0245, UID0109 | UID0237, UID0007 |
| W0-7 | `show_table` | non-table line message, part hint, figure caption, --long hint, 0-based --from-row help | UID0094, UID0037, UID0214, UID0154, UID0029, UID0133, UID0148, UID0112, UID0077, UID0211, UID0225 | UID0154 |
| W0-8 | `show_table` | header-shift repair with a strict trigger and both mappings printed; date-column label repair | UID0165, UID0148, UID0213, UID0214, UID0077, UID0228, UID0102, UID0243, UID0101, UID0225 | UID0213 |
| W0-9 | `show_table` | NOTES printed in --raw | UID0028, UID0027, UID0225, UID0148 | — |
| W0-10 | `show_table` | sibling year anchoring (YEARS ANCHORED from <file:line>) | UID0028, UID0029, UID0027, UID0117 | — |
| W0-11 | `printings` | SUMMARY first, month keys exact unless --all-periods | UID0224, UID0062, UID0077, UID0155, UID0135, UID0114, UID0122, UID0243, UID0101 | UID0155 |
| W0-12 | `find_tables` | narrative section index ([section] block) | UID0215, UID0133, UID0183, UID0204, UID0037 | — |

The union of targeted items (`EXPOSURE_ITEMS`) has **45** ids, all in the subset; the **5 untargeted subset questions**
are UID0019, UID0030, UID0096, UID0230, UID0240. "Excluding the targeted items" therefore leaves 5 questions per arm:
that column is reported, and it cannot carry a claim on its own. The SEED texts were likewise written from the same
50 questions' v9 traces (W0-PROPOSAL §(a)-(c)); the parity checklist (§5.4) is what makes the prompt W0 carry the
same knowledge as the harness W0 in words.

## 3. Outcome, metric, noise band

- **Primary:** strict `reward.py` (tolerance 0; `vendor/officeqa/reward.py` sha256
  `0d91698c87df6d889339aac36f63ae0966607f169890b0bf8b472b26bfe8138f`, the copy `data.py prepare` pins as
  `prepared/private/reward.py` and `freeze.json` records as `vendor_reward_sha256`) score of the sealed final answer
  (`final.md`; empty = 0; no fallback answer enters the metric), per arm, on all 50 and on the 45 undisputed
  questions (the same sign required on both). Secondary: 1 % tolerance.
- **Descriptive status.** One repeat at T = 0 cannot separate the expected effects (0–3 questions per 50 in the v9
  effect review) from same-configuration flips (v8/v9: ~27 % of items flip between identical configurations; SD of a
  difference of counts ≈ 3.6). A paired difference within ±4 questions is reported as noise; ≥ 8 is described as more
  than noise; nothing in between is called an effect. No hypothesis test is run on this data.
- **Reported per arm and per block** (`report.py`, `full_audit.py`, `cost.py`): strict correct per block (prequential
  curve); paired discordant counts per named contrast; the contrasts on all 50 and excluding the targeted items;
  discordance tables against v9 a (31/50), b, b2 (26 questions, §7), v8 none and Opus, labelled as cross-day
  comparisons carrying provider drift; learner rounds (proposals by kind, screen rejections by reason, replay
  statistics, verification runs by role with per-run strict and out_tokens, admitted notes and revisions with tier,
  files, patches, diff size); harness-evolve vs harness-static discordance split by replay-affected / unaffected
  episodes and by revision (computed after the campaign by the analysis, scope decision 2); manifest-tool invocation
  and friction counts per arm; `observation_coverage`; loop notes, re-notes and hard blocks; compactions; cost by
  transport origin and the $0 `replay_seconds` group.
- **Per episode:** task id, block, arm-neutral run directory, served adapter revision (`arms.revision`), notes shown,
  controller counters, parser mode, sealed answer hash, strict and 1 % grades after the final seal.

## 4. Go / no-go for the next step (declared now)

- If `d` − `e` ≥ 4 and `d` − `c` ≥ 4 with at least one admitted tool revision that the post-campaign replay marks as
  affecting ≥ 3 later episodes: proceed to a confirmatory design on unseen questions (non-Pro OfficeQA or
  SpreadsheetBench), 3 repeats, GLMM as in the v9 PREREG.
- If `d` admits no tool revision in 10 blocks: the calibration record (§6) decides whether the pipeline (screens,
  replay, judge) or the learner blocked it; fix the pipeline before any further spend.
- If `c` − `a` < 4: the hand-built W0 did not transfer even on the development set; revisit the tools before evolving
  them.
- Repeats of `c` / `d` / `e` are the first spend of any later budget.

## 5. Pinned at freeze

### 5.1 Run parameters

| item | value |
|---|---|
| profile / contract | `v10` / `v10.0` |
| roots | `v10-r1-a` … `v10-r1-e` (§1), barrier `v10-r1-barrier` (timeout 43,200 s) |
| order_seed / repeat | `v10-officeqa-pro133-order` / 1 (`arms.question_order`: ascending SHA-256(order_seed NUL `repeat=1` NUL task_id) over the base order; the subset runs the full order with the other questions removed; blocks of 5). The resulting 50-question order has `arms.order_sha256` = `6907b3da08f0a31faefcf6fbbd78385c42aa26284e6d33a3190652354c00cf5d`, registered by every root in the barrier's `order.json` (`preflight.offline`; the barrier refuses a root whose order differs) |
| actor | Codex CLI `codex-cli 0.155.0-alpha.9.2` (`frameworks-v8/codex-0.155.0-alpha.9.2/codex`, sha256 `9280c0754e8f1f6b72f495d30c8c82a006dbc4995bf0492916fa0901f6bfd1f9`; `freeze.json.codex_sha256`), model `accounts/fireworks/models/glm-5p3-flash`, temperature 0, no top_p sent, reasoning effort high, `ACTOR_SECONDS` 3,600, auto-compact 244,800, multi-agent off, permissions text off |
| frozen runtime | `frameworks-v9/actor-python` (deep manifest check by `kit.verify_frozen(deep=True)`) and `frameworks-v9/bin/rg` |
| tables index | `otables-index-v10` (`frameworks-v10/officeqa-tables-index/tables.sqlite`, 151,220,224 B; recorded as `tables_index` in every root's `.task-paths.json` by `run.py roots`, installed by `data.py prepare`, which refuses any other index version, verified against the corpus manifest by `data.verify_prepared` and for its version by `preflight.py`). The frameworks-v9 index (v9.1) has no narrative section tables: the v10 tools open it without error and W0-12 would print nothing, which is why the version is pinned (integration finding 2026-09-29) |
| environment | the L0 repair only (DESIGN §1); the README environment section and the search-helper example carry W0-CONTENT §3 |

### 5.2 Subset (`pilot/first50.json`, `arms.subset_sha256` = `c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d`)

UID0007, UID0019, UID0027, UID0028, UID0029, UID0030, UID0037, UID0044, UID0057, UID0062, UID0077, UID0094, UID0096,
UID0097, UID0100, UID0101, UID0102, UID0109, UID0112, UID0114, UID0117, UID0122, UID0130, UID0133, UID0135, UID0148,
UID0149, UID0154, UID0155, UID0165, UID0170, UID0180, UID0182, UID0183, UID0188, UID0204, UID0211, UID0213, UID0214,
UID0215, UID0218, UID0220, UID0224, UID0225, UID0228, UID0230, UID0237, UID0240, UID0243, UID0245.

Issue-47 ids in the subset (scored, withheld from every learner): **UID0007, UID0154, UID0155, UID0213, UID0237**
(`packs/officeqa/pack.py: DISPUTED`; the 18-id list `learner_common.OFFICEQA_ISSUE47` is the withholding rule).

### 5.3 W0 adapters (built by `arms.py build`, pinned by `check_w0`)

| | harness W0 (`adapters/harness-w0`) | prompt W0 (`adapters/prompt-w0`) |
|---|---|---|
| revision (`arms.revision`, sha12 of the tree minus `adapter.json`) | `02948081b850` | `720b7ba30245` |
| `SEED.md` | 4,053 B, sha256 `edbe68e7e0012f088d6d9f6051e9c674f45747ec1bbd6ce9f8c03f611a5eb4c4` | 4,092 B, sha256 `b8a6cb1bf6b4be91d94efd5431814eadbd2bae88efae458b0dfb1b8b27e76b86` |
| `controls.json` | 165 B, sha256 `f3dd2fcf434e801aaafba383b0aef8e8bdb2e447725de6340ad522615802f42a` | — |
| `tools/USAGE.md` | 2,512 B, sha256 `912fdd6e1c5e2b902afecdf98abb164502e80bf01113dd5300483d02aa7fd780` | — |
| `tools/manifest.json` | 379 B, sha256 `e52b9404ad7dfb767f41075f92c40cd7c1edf1528ebf6f750e8527e000b18c4a` (`find_tables`, `show_table`, `printings` → `cli.py`; `calc` → `calc.py`) | — |
| `tools/cli.py` | 8,662 B, sha256 `72194fb7cc2015e0293a48e348cc1a695fc7cb946ce6b7caee81d2373f13ccf0` | — |
| `tools/otables.py` | 172,323 B, sha256 `cc42041179fdb3eddf2154a0be5726ff8bc96495f19d5af142beeba7ee28d309` | — |
| `tools/calc.py` (pinned to `harness_core/tools/calc.py`) | 4,467 B, sha256 `1d1a07005da75c47a8c2679b8ac908de1b6236d42f8d060075c5a15e39a84ea8` | — |
| notes | none (`notes/.keep`) | none |

`adapters/harness-w0/tools/{cli.py,otables.py}` are byte-identical to `packs/officeqa/tools/` (the frozen observation
parser `packs/officeqa/observe.py` parses exactly these tools' options). The W0 tools are the W0-PROPOSAL §(d) changes
W0-1..W0-12 with the fix pass of `docs/w0/W0-BUILD-REPORT.md` plus one integration fix (2026-09-29, `_period_range`:
labels tying on the period key are ordered by text, so the `printings` no-match line no longer varies between
processes; found by the replay of §5.8).

### 5.4 Model-visible bytes per arm and the parity checklist

| arm | SEED.md | USAGE.md | controller templates | total | budget |
|---|---|---|---|---|---|
| `plain` | 0 | 0 | 0 | 0 | 16,384 |
| `prompt-evolve` (and `prompt-static`) | 4,092 | 0 | 0 | 4,092 | 16,384 |
| `harness-static`, `harness-evolve`, `harness-evolve-notes` | 4,053 | 2,512 | 320 | 6,885 | 16,384 |

Controller templates (gate off, `controller_runtime.fixed_templates(False)`): `loop_template` 72 B, `loop_options`
87 B, `loop_escalate` 62 B, `hard_block` 99 B = 320 B (`arms.harness_fixed_text` = `learner_common.controller_template_bytes`,
pinned by a test). Recorded, not counted: the hard block's echo cap `hard_block_echo_cap` = 2,000 B (the echo repeats
the model's own tool output, §5.6). Notes admitted later add their rendered bytes (≤ 600 B each, ≤ 3 shown per
question); `USAGE.md` ≤ 3,072 B and every line names a manifest tool or a `--option` of the tool sources.

Parity checklist (`packs/officeqa/SEED_KNOWLEDGE`, 13 items, each present as one sentence in both SEEDs; both SEEDs
≤ 4,096 B and within ±25 % of each other — 4,053 vs 4,092; the prompt SEED names none of `find_tables`, `show_table`,
`printings`, `calc`): `compute_with_code`, `provenance`, `answer_count`, `answer_form`, `alternatives`,
`latest_printing`, `ocr_variant`, `period_match`, `flagged_rows`, `table_choice`, `entity_column`, `corpus_boundary`,
`narrative_sections`.

### 5.5 Controls (`adapters/harness-w0/controls.json`, fixed at W0 this round; `contracts.checked_controls`)

`{"loop_guard": true, "loop_threshold": 3, "loop_notes_max": 3, "splice": true, "ledger_splice_bytes": 8000,
"gate": false, "enforce_requery_max": 0}` = `CONTROLS_DEFAULTS`. `CONTROLS_RANGES`: `loop_threshold` [3, 6],
`loop_notes_max` [1, 5], `ledger_splice_bytes` [2,000, 8,000], `enforce_requery_max` [0, 1]; booleans `loop_guard`,
`splice`, `gate`. A `controls.json` that differs from W0 is refused by `check_adapter` / `full_audit`.

### 5.6 Controls behaviour change applied at integration (W0-PROPOSAL §(e) items 1 and 2; item 3 deferred)

Relative to DESIGN §3 as built in round 2 (one loop note per command; one hard block per command):

1. **Re-noting.** A command is noted when it reaches `loop_threshold` (3) issues and again after every further
   `loop_threshold` issues (the count at the last note is kept per command); notes past `loop_notes_max` escalate as
   before.
2. **Hard block on every identical repeat, with an echo.** Gate off, once `loop_notes_max` (3) notes were shown,
   *every* further issue of a noted command that repeats an output it already showed (same normalised command, same
   normalised-output sha256) gets the block note `blocked: identical output already shown for: <cmd> (call N, k
   repeats)`, then the first 20 lines (≤ 2,000 B) of that output, then the gate-off options line; a repeat whose
   output changed falls back to rule 1. Each block is a sticky note and a `hard_block` decision row (with `repeats`,
   `first_call`, `echo_bytes`).
3. `empty_output_retry` (§(e) item 3) is **not** built.

The templates' bytes are in §5.4; the echo is bounded by `HARD_BLOCK_ECHO_LINES` = 20 / `HARD_BLOCK_ECHO_BYTES` =
2,000. Tests: `tests/test_v10_broker.py` (unit and CodexSim), `tests/test_v10_codex_live.py` (the pinned Codex
binary), `tests/test_v10_e2e.py` (toy campaign).

### 5.7 Learner constants (`learner_common.py`; identical for both learners unless stated)

| constant | value |
|---|---|
| model / reasoning effort / call budget | `accounts/fireworks/models/glm-5p3-flash` / `high` / 3,600 s per learner call |
| session budget (`Budget`) | 8,000,000 input tokens, 200,000 output tokens (raised from 2,000,000 / 120,000 after the paid calibration, §6), 400 calls, checkpoint at 600,000 tokens, 3 runaway resends, 65,536 max tokens per call |
| verification | K = 2 re-runs per question; KEEP = 2 controls per note; KEEP 2 (`additive` tier) / 4 (`value-changing` tier) per revision; SUPPORT_MIN 1, SUPPORT_MAX 3 (`contracts.SUPPORT_MAX`); COST_MAX 3.0 (notes only; recorded, not gating, for revisions); CONCURRENCY 8; judge = cards `block.judge` |
| slots per block | ≤ 2 notes (`B_MAX_NOTES`) + ≤ 1 revision (`B_MAX_REVISIONS`; a bundle uses the revision slot); cost bound ≤ 34 verification actor runs per block |
| proposals | PROPOSAL_ATTEMPTS 20 per session; PATCHES_MAX 20; TRY_TOOL_MAX 30 (`TRY_STDOUT_CAP` 8,192 B); REPLAYS_MAX 4 per session (USAGE.md-only proposals excluded) |
| replay | REPLAY_JOBS_MAX 80 (every invocation of the support episodes first, then the most recent of the others), REPLAY_WORKERS 4, REPLAY_JOB_SECONDS 20 (CPU and wall), REPLAY_WALL_SECONDS 300, REPLAY_MIN_SOURCE_OK 5; `calc` skipped; regressions (exit 0 → ≥ 2, timeout, crash) and first-line contract breaks refuse; `expect` must change an output |
| notes | NOTE_MAX 600 B; ≤ 3 shown per question; NGRAM 8 (question copy); retirement RETIRE_MIN_TRIGGERED 4, RETIRE_GAP 0.20 |
| text / tools caps | TEXT_BUDGET 16,384 B; SEED_MAX 4,096 B; USAGE_MAX 3,072 B; ≤ 12 python files, ≤ 196,608 B per file, ≤ 393,216 B total, new file ≤ 16,384 B; `calc.py` pinned |
| ToolRunner (replay, `try_tool`, preflight) | `campaign.make_tool_runner` (one constructor): a neutral copy under `runs/code/<tools sha12>/tools`, the actor kit's environment keys pointing into the per-call work dir, cwd named `workspace`, exactly the script's import closure readable, reads of `prepared/agent/corpus` and `prepared/agent/tables` only (never `evidence/`, `private-evaluation/`, `prepared/private/`, `runs/`, `evolution/`), no network, no process creation, 20 s CPU / 20 s wall per run, 16 KB output cap, resident memory cap 3 GB (`memory_mb`, a process-group watchdog: macOS has no enforceable RLIMIT_AS) |

### 5.8 Screens and replay thresholds

- Screen version hash (`learner_common.screen_version()`, sha12 of `screen_staged`, `python_findings`,
  `code_leak_findings`, `text_leak_findings`, `agrees_with_reference`, `changed_lines`, `tool_invocation`,
  `fold_constant`, `file_words`, `data_like` and the constants below): **`39a314bf114b`** (was `aceb0ce91240`
  before the calibration fix, §6). `learner_common.py` sha256
  `f4e98bbfaef7861cd246edf4bdef9f287b1ef89d372a9b5b64cf6df7c9d36257`; `packs/officeqa/leak.py` sha256
  `287becec88abfeb23a355265eeb4dce32411fbe70551bebf5d6bc0828fb1f75b`.
- Banned calls `exec`, `eval`, `__import__`, `compile`; process calls (`os.fork/forkpty/system/popen/posix_spawn*/
  exec*/spawn*/startfile`) refused; modules refused as useless / process-creating: `socket, ssl, http, urllib,
  ftplib, smtplib, xmlrpc, asyncio, multiprocessing, ctypes, importlib, pickle, shelve, marshal, subprocess, pty`;
  stage path `^tools/(?:([a-z][a-z0-9_]{0,31})\.py|USAGE\.md|manifest\.json)$`; reserved tool names `python`,
  `python3`, `rg`, `submit` and any name on the actor's `BASE_PATH`; arm-revealing fragments `plain, text, mech,
  static, evol, rule, control, prompt, card, learn, seed, note, adapt, mwm, baseline, harness, revision` (delta rule
  inside a source file: only words new to the file count; full check on file names, manifest names, USAGE.md, notes,
  proposal ids and the code-root path); numeric-literal rule: a new literal or constant-foldable expression (incl.
  `int/float/str/ord/len/abs/round/chr` and string concatenation) equal to a released reference or agreeing with one to
  ≥ 3 significant digits (not a year, not in `SMALL_CONSTANTS` 0–12, 52, 100, 365, 1000) is refused; new string
  literals > 64 chars that look like data (digits > 25 % of non-space characters or > 4 numbers) and base64-looking
  tokens refused, and every number inside a new string literal goes through the numeric-literal rule (calibration
  fix, §6); string expressions > 64 chars refused; new numeric dict/list literals refused.
- Preflight replay of the v9-a evidence against the unchanged W0 (source = candidate = W0, the determinism check;
  `preflight.py --replay-v9a ../v9-p1-a`, `preflight/replay-v9a.json`): threshold ≥ 200 rebuilt invocations and 0
  changed outputs. Result on the pinned W0 (2026-09-29): 3,235 recorded commands, **724 rebuilt** (285
  `find_tables`, 274 `show_table`, 165 `printings`), 721 exit 0 (3 exit 2 in both runs, as in the pilot record),
  **0 changed**, 0 timeouts — passed twice: over the v9-a root's own v9.1 index (141 s) and, the record kept in
  `preflight/replay-v9a.json`, over the campaign's v10 index (`--replay-tables-index`, 144 s; the section tables
  of W0-12 exercised). (The first run found one `printings` no-match line varying with the hash seed; fixed in
  `otables._period_range`, §5.3, and re-run.)
- W0 idempotence: every W0 tool file, USAGE.md and manifest.json restaged unchanged passes `screen_staged` with zero
  findings (`preflight.w0_idempotence`; `calc.py` skipped as pinned).

### 5.9 Analysis and record hashes

- Analysis digest builder `officeqa-v10-analysis/build_digests.py` sha256
  `a264347786cdde579b2b0f5aa7e548c103615a5123a5e852c444633064e6af89`. The post-campaign analysis scripts (contrast
  tables, replay-affected split) are added in a dated addendum with their hashes before they run.
- `freeze.json` per root records: every top-level source file, `harness_core/`, `packs/`, `adapters/` (tree
  revisions), `PROTOCOL.md`, `PREREG.md`, `.task-paths.json`, the Codex binary, the frozen runtime manifest, the
  tables index, the question order, both W0 revisions, `controls_defaults` / `controls_ranges`, `vendor_reward_sha256`;
  `preflight/readiness.json` records the same source hashes and this file's sha256.

## 6. Calibration (DESIGN §5 "Calibration before the paid run")

One `harness` learner session on the v9-a evidence (`calibrate.py --source-root ../v9-p1-a --actor-runs 20`: the 50
released episodes as one block, issue-47 references never written, W0 tools as the source revision, the campaign's
ToolRunner, a verification runner capped at 20 real actor runs; the scratch root links the v9-a root's `prepared/`
entry by entry with the campaign's v10 tables index in place of the root's own v9.1 one, `calibrate.py
--tables-index`, default the study index, recorded in the record's `block`) answers, before any paid campaign call,
whether the pipeline can admit a tool change at all: does the learner stage a change (the SUMMARY `%g` fix, the caption lookback,
a `--grep` hint or anything else), do the screens, the replay and the paired re-runs admit it? The $0 dry run of
2026-09-29 (`preflight/calibration.json`, `dry_run: true`: a scripted learner proposing nothing) exercised the
evidence build, the source adapter check, the ToolRunner and the record; it is not the calibration.

- **Calibration outcome:** [FILL AT FREEZE] [DECISION PENDING] calibration outcome from `preflight/calibration.json`
  (`dry_run: false`): proposals by kind, screen outcomes by reason, replay status and counts, tiers, verdicts,
  admission, actor runs used, cost; and the expected number of tool proposals / admissions per block that follows
  from it (the analysis-derived fixes are already in W0 — 4 of 13 per-question analyses named a TOOL fix — so few
  discoverable targets remain; the calibration separates "the learner cannot do it" from "the pipeline never let it").

## 7. Statements carried from PROTOCOL.md §8 and the build reports

- **Tool processes.** A tool process started by the actor runs under the actor's Seatbelt profile: no reads outside
  the granted roots (the corpus, the tables index, the script's import closure), writes only in the workspace, TCP
  only to the actor's own broker port (which Codex's own shell can reach as well). The nested per-tool network denial
  is deferred (scope decision 4). The learners' replay and `try_tool` run tools in the ToolRunner profile (§5.7) from a
  neutral copy with an actor-like environment; the replay is a functional screen, admission rests on the paired actor
  re-runs.
- **Model-visible names.** No name the model can see carries an arm word: root names, `runs/episodes/NNN`,
  `runs/verify/BB/rNN/NN`, `runs/code/<sha12>/tools`, `.kit/`, `[runtime:<reason>]` inserts, tool file and manifest
  names (`arms.ARM_REVEALING`, checked at creation, in the E2E request bodies and by the audit).
- **Residual exposures (build round 2 Known gaps 4, 5, 7).** (4) The code-aware diff leak screen is best-effort: it
  folds literals, arithmetic, string concatenation and the builtins above; a released reference encoded through data
  the tool reads at run time, or through a comparison with a corpus value, is not caught. Support questions are
  released wrong episodes, so a tool that special-cases them can pass the paired re-runs; the `d` vs `e` contrast is
  read with that residual in mind — the post-admission diagnostic (re-running each support question's commands and
  flagging output lines that print the released reference verbatim) is not built this round. (5) The broker does not
  reject non-Codex `/v1/responses` bodies; a tool process could in principle reach the broker port, as Codex's own
  shell can. (7) The replay cwd is `<tmp>/tool-XXXX/workspace`, not `runs/episodes/NNN/workspace`: a tool inspecting
  the parent of its cwd can tell the replay from the actor (process creation and network differ by design).
- **Deferred by scope (not built, not gating):** the grounding judge (recorded only), `propose_revert` and
  prequential revision monitoring (affected-episode accuracy is computed by the analysis after the campaign), note-only
  variant runs of admitted bundles, nested `sandbox-exec` in tool wrappers, the fine parser mode (coarse: any tool sha
  differing from W0 switches the whole episode to header mode), `mechanism_learning.py`.
- **b2 caveat.** The v9 pilot-2 root `v9-p2-a` (`mech-static`, T = 0) was stopped after **26** scored episodes
  (`state.json`: 26 results, 26 episode directories; the design documents round this to 25), so every discordance
  table against b2 covers those 26 questions only, on a different day (provider drift).
- **Order of paid steps** (each needs the owner's explicit approval and the Fireworks key, never written anywhere):
  calibration (§6) → fill the two slots → hash this file (`preflight.py` per root records it) → readiness smoke per
  root (`preflight.py`, cents) → `run.py run` per root (the five started together; the barrier pairs their blocks).

## Addenda (dated; after hashing)

(none yet)
