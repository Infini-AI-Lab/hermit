# v9 readiness: the gate before the first paid call (S8 review, 2026-09-28)

## 中文摘要（给用户）

**结论：**
- **(1) 约 $1 的付费 smoke：代码层面没有阻碍，可以做。** 只差你的批准和 API key。
- **(2) 20 题 pilot：还不能做。** 缺 4 块代码和 2 个你的决定（见下）。

**先解释几个词：**
- **smoke**（冒烟测试）：只跑一道合成的假题，确认"Codex → broker → Fireworks → 回来"这条链路在真 API 上能通。它不测准确率。
- **pilot**（预实验）：PREREG §7 规定的 20 道真题 × {text-static, mech-static}，外加一个温度对照格。它的输出是 go/no-go（继续/停止）判定。
- **$0 rehearsal**（零成本彩排）：用真 Codex 程序 + 假模型（127.0.0.1 本地假服务器）把整条流水线跑一遍，用来在花钱之前找出接线错误。

**我跑了什么、结果如何：**
- 修复之前，整套测试（fast + slow + live_codex + e2e）：**390 passed，0 skipped，0 failed**，308 s。
- 修复之后重跑：默认档 **329 passed、61 skipped**（61 个是需要手动开启的 slow/live 测试），94 s；全开档 **390 passed，0 skipped，0 failed**，308 s。
- 我还用本地假模型，按付费 smoke 的原命令把 **5 个 arm 全部**跑了一遍（S6 的测试原来只覆盖 plain 和 mech-static）：**5/5 通过**。共 12 次 upstream 调用。按记录的 token 数估算，真 smoke 大约几美分。

**我修了 4 个小缺陷（详见 §3）：**
1. **readiness 永远过不了（真阻碍，已修）。** `preflight.py` 会在每个 campaign root 里跑 orchestration 测试。但有一个测试断言"这个目录没有 campaign"，而每个 root 都有 campaign，所以 readiness 永远不会通过，正式 run 也就永远启动不了。我在一个模拟 root 里复现了它，然后修好。
2. preflight 的 $0 live 档加入了 S8 的端到端彩排。我在模拟 root 里单独跑过这个彩排：12/12 通过。
3. `full_audit` 审计一条"完整准入"的规则时，原来只检查记录"存在"，现在还要求记录显示"通过"。
4. 文档与代码对齐：
   - `PROTOCOL.md` 补写 4 件事：Codex 每次请求里那句"文件系统不受限"的错误描述；规则准入的三种模式、阈值和数量上限；审计按轮次判断；成本在传输故障后怎么读。
   - `arms.py` 示例里的 order_seed 改成与 PREREG 一致。

**付费 smoke 你需要知道的：**
- mech 两个 arm 会收到 1 条 gate finding："题目要 1 个数值，你的答案里有 0 个"。原因是合成答案 `TRANSPORT_xxxx` 里没有可解析的数字。这是预期现象，不影响通过判定。
- smoke 只测 actor 链路，**不测 learner**（学习器走的是 Fireworks 的 chat/completions 接口）。pilot 只用 static arm，不需要 learner。但正式 campaign 之前需要单独做一次 learner smoke。

**pilot 前必须补的（代码）：**
- (a) 能在 20 题子集上运行。现在 data/campaign/grade/audit/preflight 都把"133 题"写死了。
- (b) 温度成为可配置项。现在 broker 写死 `temperature=0`。
- (c) go/no-go 指标计算脚本：
  - ledger 覆盖率；
  - 答对 vs 答错的 episode 上 finding 的触发率；
  - finding 之后错→对和对→错的翻转次数（需要在 controller 侧给提交过的中间候选答案打分）；
  - 门限检查；
  - 提交率和 re-query 率。
- (d) 20 道"容易循环"题的选题脚本和题目清单的 hash。只用 v8 轨迹里"同一命令重复 ≥3 次"来选，不看答案。

**pilot 前必须由你决定的：**
- 温度对照值："model-card value" 具体是多少。
- pilot 的 root 布局和预算上限。

**建议（不阻碍 pilot）：**
- 修 broker 的"卡住的流在 episode 结束后仍占线"问题（S3，request 4）。

**安全边界：** 本次审查没有调用任何付费模型，没有读取任何密钥，没有打开任何参考答案。

---

## 1. What this gate checked

Paths are relative to `officeqa-pro133-v9-plumbing/` unless absolute.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

1. Reviewed `wave2/S8-e2e.md` and `tests/test_v9_e2e.py` + `tests/e2e/*`. The rehearsal is what it claims to be:
   - all five arms run the production episode path (prepare → real broker/controller → pinned Codex under Seatbelt);
   - real S5 learner rounds;
   - seal/release;
   - audit/cost/report.

   The declared deviations (§5 of S8) are honest. The one that matters for reading results is `MIN_RIGHT`/`MAX_RIGHT_FIRE` relaxed only in the learned-rule scenario. The main rehearsal uses the declared constants and shows a rule rejected for `only 3 right episodes (< 10)`.
2. Ran the entire suite at every tier (§2).
3. Ran the paid-smoke code path at $0 for **all five arms** (`scratchpad/s8r_smoke5.py`: `run.smoke(..., ['plain','text-static','text-evolve','mech-static','mech-evolve'])` against a local fake upstream).
4. Ran the preflight test suites **inside a simulated campaign root**: a copy of the plumbing named `v9-r1-a`, with `.task-paths.json` arm `mech-static`, in the scratchpad. The goal was to find what breaks when `preflight.py` runs them in a real root.
5. Compared `PROTOCOL.md` / `PREREG.md` with the code: constants, settings, arms, seed rules, W0 sizes, learner constants, audit rules, cost fields.

## 2. Test results (exact commands, run from `officeqa-pro133-v9-plumbing/`)

| command | result |
|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests -rs --durations=25` (before my fixes) | **390 passed**, 0 skipped, 0 failed, 308 s |
| `$PY -B -m pytest -q -p no:cacheprovider tests` (after my fixes) | **329 passed, 61 skipped** (the opt-in slow/live groups), 95 s |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` (after my fixes) | see §2.1 |
| `$PY -B arms.py check` | text-w0 2,478 B; mech-w0 2,429 B (829 SEED + 1,130 rule templates + 470 controller; gate frame 275 recorded separately), both ≤ 16,384. Matches PROTOCOL/PREREG |
| `$PY -B cost.py --self-test` | 13 checks, `paid_model_calls: 0` |
| `$PY -B scratchpad/s8r_smoke5.py <dir>` (the paid smoke path, 5 arms, fake upstream, real Codex) | **passed 5/5**; mech arms: `submits=1`, `gate_rounds=1`; 12 upstream calls; `cost.estimate` on it: 12 logical calls, 0 unknown usage |
| suite inside the simulated root `v9-r1-a` (mech-static), default + `V9_LIVE_CODEX=1` | before the fix: `test_experiment_constants_without_a_campaign` fails in **any** root. Every other failure there was an artefact of my scratch layout: the root must sit next to `opus-officeqa-133/`, `officeqa-pro133-v8-plumbing/` and `adapter-comparison-work/`, as PROTOCOL §3 requires, and I could only symlink those |
| `-m live_codex tests/test_v9_e2e.py` inside the simulated root | **12 passed**, 87 s |

The slowest tests are the v8 replay goldens (38 s), the e2e adversity scenario (34 s), the e2e crash/resume (20 s) and the e2e main group setup (20 s). None is over 60 s, so the `slow`/`live_codex` markers are correct.

### 2.1 Full run after the fixes

`V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests`: **390 passed**, 0 skipped, 0 failed, 308 s (every tier: fast, slow, live_codex, e2e).

## 3. Defects fixed in this review (each small, each re-tested)

| # | file | defect | fix |
|---|---|---|---|
| R1 | `tests/test_v9_orchestration.py` | `test_experiment_constants_without_a_campaign` asserted `experiment.CAMPAIGN is None`. `preflight.py` runs this file (suite `orchestration`, and the `live_codex` tier) inside every campaign root, and every root's `.task-paths.json` names an arm. So `suite_orchestration` fails in every real root, readiness never passes, and `run.py run` refuses to start. Reproduced in a simulated root. | Branch on `paths._config`: a root must have `CAMPAIGN.arm == ARM`; the plumbing must have none. Every other constant is still asserted. |
| R2 | `preflight.py` | The preflight `$0` live tier did not include the S8 end-to-end rehearsal, the strongest $0 check of a root's code copy. | `LIVE += tests/test_v9_e2e.py` (≈ 90 s per root; verified passing inside a simulated root). |
| R3 | `full_audit.py` (`_admission_matches`) | A `full`-mode rule admission was accepted when `counterfactual` and `precision` were merely non-empty. A recorded but failed verdict would have passed the audit. Only admitted edits are served today, so this is defence in depth. | Require a counterfactual row with `admit: true`, `precision` present and not `ok: false`, and `admitted` not false. Pinned by 4 new asserts in `test_audit_accepts_every_declared_rule_admission_mode`. |
| R4 | `PROTOCOL.md` | The text lagged the code (S8 requests 1, 6, 7; S5 review R4). | §2 L0: declared Codex's false `<permission_profile type="disabled"><file_system type="unrestricted" />` line. §2 Learners: admission modes and constants (Wilson bound ≤ 0.20 on ≥ 10 right, ≥ 1 wrong fire; 16 right episodes before any new rule; 2,000 ms engine runtime; `MAX_EDITS` 2 by net effect; 20 shared proposals; mech-arm notes verified without same-block edits, marked **[user]**). §3: per-round audit of admission modes; cost after transport faults; the open stalled-stream record gap. |
| R5 | `arms.py` (docstring) | The example `order_seed` `v9-officeqa-order-1` differed from PREREG's `v9-officeqa-pro133-order`. | Aligned. The seed is not enforced by code; the operator must copy PREREG's value into every root. |

## 4. Protocol text vs code: what I checked

Everything below matches the code unless marked.
- Codex settings. `acting.V9_SETTINGS` / `V9_DECLARED_SETTINGS`:
  - multi-agent off, agents off;
  - `model_auto_compact_token_limit` = 244,800;
  - `include_permissions_instructions=false`;
  - `model_context_window` = 1,048,576, the same as v8 FULL.
- `experiment`:
  - `ACTOR_SECONDS` = 3,600;
  - `BROKER_OPTIONS` unbounded;
  - `REQUERY_WHEN_CLEAN=False`, passed by `campaign.build_controller`;
  - `TEMPERATURE=0`, and `broker.normalized` sends 0.
- Model string `accounts/fireworks/models/glm-5p3-flash` (broker, client, learner_common, data). Codex pinned at `frameworks-v8/codex-0.155.0-alpha.9.2`.
- Arms table, `.task-paths.json` keys (campaign + path keys; others refused), `ARM_REVEALING` list, `ISSUE_47` (18 ids; 115 undisputed).
- Ten seed rules as listed; `policy.json` = `POLICY_DEFAULTS`; W0 byte counts.
- Learners: `K=2`, `KEEP=2`, `SUPPORT_MAX=5`, `CONCURRENCY=8`, `B_MAX_NOTES=2`, same model/effort; grader argument order is consistent (`Grader.strict(answer, reference)` → `score(reference, answer)`).
- **Not enforced by code (operator discipline):**
  - `order_seed` equal to PREREG's value;
  - "every root carries the same PREREG hash" (each root checks its own).
- **Not in code at all:** the whole of PREREG §7 (pilot), see §6.

## 5. Blockers for (1) the ≈ $1 paid smoke: one synthetic task per arm

**No code blocker.** The exact production path passed at $0 for all five arms.

Preconditions:
- your explicit approval;
- the Fireworks key in `FIREWORKS_API_KEY`, or typed at the hidden prompt. It is never written.

Command, from the plumbing (no root needed):

```
cd /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
$PY -B run.py smoke --name smoke-01 --arms plain text-static text-evolve mech-static mech-evolve
```

What it does:
- One synthetic question per arm: read `note.txt`, return its `TRANSPORT_<hex>` string. Controller arms also `submit` it.
- The arms run in sequence, each ≤ 3,600 s.
- Pass requires, per arm:
  - `completed`;
  - answer = marker;
  - kit intact;
  - ≥ 1 shell command;
  - no transport failure;
  - reasoning effort `high` forwarded on every Codex call;
  - controller arms: ≥ 1 submit and `fallback_answer` = marker;
  - source files unchanged.
- Output: `preflight/smoke-01/verification.json`.
- Expected cost: cents. Each request is ≈ 31–34 KB (≈ 9 K tokens) at $0.15/M input and $0.50/M output. Read `known_cost_lower_bound_usd` if any call had a fault.

What to expect:
- The mech arms get one `answer_count` finding: "The question asks for 1 value(s); your answer has 0". The pack parses no number in `TRANSPORT_<hex>`. This is harmless: the pass criterion accepts a plain final or a `--keep` resubmit. If GLM instead rewrites the marker, the smoke fails and says so; re-run.
- What the smoke does **not** cover:
  - the learner transport (Fireworks chat/completions with `reasoning_effort='high'`, `max_tokens`, 5 attempts, completion-overrun rejection);
  - an enforcement re-query and a compaction splice against the real provider;
  - a stalled stream.

## 6. Blockers for (2) the 20-question pilot (PREREG §7)

**Code missing (blocking):**
1. **Subset runs.** The loop is hard-wired to all 133 questions:
   - `data.verify_prepared` requires `sample_count == 133` and the v3–v8 seed;
   - `data.grade` / `seal_predictions` require the full manifest;
   - `preflight` checks `question_count_133`;
   - the audit expects the full order.

   Needed: a declared pilot subset (list + sha in `.task-paths.json` or the manifest), a prepared pilot root layout or a subset filter, and grading/audit on the subset.
2. **Temperature is not configurable.** `broker.normalized` hard-codes `temperature=0`, and `campaign` refuses a mismatch with `experiment.TEMPERATURE`. The temperature cell needs a broker option fed by a declared `.task-paths.json` key, recorded in the freeze and checked per request by the audit.
3. **Go/no-go metrics are not computed anywhere.** A pilot analysis script needs:
   - ledger coverage (the final or submitted answer links to a ledger observation or calc);
   - per-episode finding rate on right vs wrong episodes;
   - wrong→right and right→wrong flips after a finding (this needs controller-side grading of the pre-finding candidate with `reward.py`, after the seal);
   - the gate limits (union fire ≤ 20 %, right→wrong ≤ 5 %);
   - submit and re-query rates.

   Every input exists: `api/controller.jsonl`, `api/checks/`, `ledger.jsonl`, grades.
4. **The loop-prone 20-question list.** It must be chosen from v8 traces by a label-free rule (for example ≥ 3 identical commands in a v8 run), written out, and its sha put in a PREREG addendum before the pilot.

**Decisions for you (blocking):**
- The "model-card" temperature value for the cell.
- Pilot roots and budget cap (PREREG says ≈ $10–20).

**Operational steps ($0, not yet done by anyone):**
- Create the pilot roots as neutral-named copies of the plumbing **next to** it (for example `v9-p1-a`, `v9-p1-b`). Each `.task-paths.json` needs:
  - `profile`, `arm`, `repeat`, `pack`;
  - `order_seed = "v9-officeqa-pro133-order"`;
  - `barrier {dir, roots, timeout_s: 43200}`;
  - `framework`, `data_cache` (v8 used `frameworks-v8/next-a48c0f9` and `baseline-work/data-cache/officeqa-official`).
- Then run `data.py prepare`.
- Then run `preflight.py --offline-only`. This has only been tested on a synthetic 133-task root, never on real prepared data.

**Recommended (not blocking):**
- Close the active upstream response on broker exit (S8 request 4). In a 60-episode pilot a stalled stream would otherwise write its records after the seal or audit.

## 7. Before full campaigns (not asked, listed for completeness)

- A paid **learner smoke** (S5 review R3):
  - `reasoning_effort='high'` accepted by chat/completions;
  - completion tokens ≤ `max_tokens`;
  - `MAX_ATTEMPTS=5` adequate.
- Opaque verification directories (S8 request 3).
- Pack-neutral `data.py`/COMMON for SpreadsheetBench (request 2).
- PREREG pending decisions: repeats, analysed-id sha, CPI data, confirmation set.
- `run.py run` does not refuse while `[DECISION PENDING` lines remain (S6 review request 7).

## 8. Files changed by this review

- `tests/test_v9_orchestration.py` (R1)
- `preflight.py` (R2)
- `full_audit.py` (R3)
- `tests/test_v9_e2e.py` (R3 asserts)
- `PROTOCOL.md` (R4)
- `arms.py` (R5, docstring only)

Report: `officeqa-v9-design/wave2/S8-e2e-review.md`. Requests: `officeqa-v9-design/wave2/S8-e2e-review-requests.md`.
Scratch (not deliverables): `scratchpad/s8r_smoke5.py`, `scratchpad/s8r/`.
