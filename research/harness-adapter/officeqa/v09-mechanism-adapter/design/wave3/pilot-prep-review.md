# v9 wave 3: adversarial review of the pilot preparation (2026-09-28)

## 中文摘要

**结论：** 实现者的 6 项工作基本属实，可以进入 pilot 前的"你来决定"阶段。我发现并修复了 3 个有把握的缺陷，都在"指标/审计会不会悄悄算错"这一类。修复后全部测试通过：**408 passed, 0 failed**（原 407 个 + 我新加的 1 个）。

**先解释几个词：**
- **label-free（不看标签）**：选题时不读任何"标准答案、评分、对错"。这里的"标签"就是答案和成绩。
- **sha256**：一种文件"指纹"。内容改一个字符，指纹就完全不同。用它来核对"跑的是不是同一份题单/同一份代码"。
- **seal（封存）**：先把模型的全部答案锁定并记下指纹，之后才允许评分。这样评分结果无法反过来影响答案。
- **ledger coverage（账本覆盖率）**：模型最终答案里的数字，能否在它自己的计算（calc）或工具输出（observation）里找到来源。PREREG §7 要求 ≥ 60%。
- **answer span（答案片段）**：从模型最终消息里抽出的"真正的答案那一行"。gate 本身就用 `extract_final` 做这件事。

**逐项核对结果：**
1. **选题脚本确实看不到答案。** 我在真实 v8 数据上重跑了选题，并用 Python 的 audit hook（审计钩子，记录进程打开的每一个文件）记录。它只打开了 399 个 `task.json` 和 399 个 `events.jsonl`。v8 的 `task.json` 只有 `ordinal / question / task_id` 三个字段，没有答案。重跑得到的列表指纹与提交的文件一致（`81d2bf20…dc56`）。
2. **子集评分/审计不会评错题。**
   - `seal` 要求预测正好覆盖子集。
   - `grade` 会核对封存里的子集指纹和 manifest 的是否一致，并且只评子集。
   - campaign 会拒绝与 manifest 不一致的子集。
   - audit 会核对题目顺序和子集指纹。
   - **缺陷 A（已修）：** audit 的"封存和评分正好覆盖本 root 的题"这一项，在评分文件缺失时会被**直接跳过**，而不是判失败。
3. **温度确实被记录和审计。** 声明值 → freeze → 每题 `invocation.json` → 每次上游请求体（包括 controller 的重问）都逐条核对。broker 的 HTTPS handler 替换与 Python 3.12 原版逐行等价。
4. **指标从不打印答案值。** 测试检查了表格、JSON 和命令行输出。官方 `reward.py` 内部吞掉所有异常，也不 print。
   - **缺陷 B（已修）：** 覆盖率用的是**整条最终消息**里的任意数字。例如消息里写着"2001 财年"，而 2001 在工具输出里出现过，这个 episode 就会被算作"已链接"，覆盖率被高估。现在改用 gate 自己的答案片段抽取。
   - **缺陷 C（已修）：** 本地重新评分与官方成绩文件不一致时，只在 JSON 里记一个 `grade_file_agrees: false`，表格里不显示，决策照常输出。成绩文件缺失时同样如此。多个 root 用了不同题单时也不报错。现在这三种情况都直接拒绝计算。
5. **broker 中止卡住的调用**：代码和测试都与报告一致。TCP 连接阶段卡住、读 HTTP 错误体时卡住，这两种情况仍然无法中止，已在 PROTOCOL §3 写明。
6. **`pilot-roots` 命令**：不做任何付费调用。附录没填完时会拒绝创建。README 里的命令与代码一致。

**你需要知道的：**
- 我改了 `pilot/metrics.py`，它的 sha256 变成 `367c2395…f0b6`，已同步更新到 `pilot/ADDENDUM.md`。实现者报告 `pilot-prep.md` 里写的旧值 `ce5d6ee3…` 已过时。
- `ADDENDUM.md` 里覆盖率定义的措辞也同步改了（答案 = 最终消息的答案片段）。
- 你的 3 个决定不变：模型卡温度、预算上限、正式实验温度规则。预算上限仍然只能手动执行，代码不会自动停。

---

Paths are relative to `officeqa-pro133-v9-plumbing/`. `PY` = `adapter-comparison-work/.venv/bin/python`.

## 1. What I verified

| claim | how verified | verdict |
|---|---|---|
| Suite passes | `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` before any change | 407 passed (317 s) |
| Selector is label-free | Re-ran `select(scan(project))` on the real v8 tree under `sys.addaudithook`. Opened files: 399 `task.json`, 399 `records/events.jsonl`, and the script itself; nothing outside the project. I checked the key set of the v8 `task.json` files only: `ordinal, question, task_id` | holds; list re-derives, `--check` → `matches_file: true` |
| Subset grading cannot grade the wrong items | Read `data.selected_task_ids` / `check_subset` / `seal_predictions` / `grade`, `campaign.load`, `full_audit.completion`. Seal requires exactly the subset; grade requires seal subset sha == manifest subset sha and manifest unchanged since the seal, then filters answers to the subset; campaign refuses spec ≠ manifest subset | holds, except the audit gap (§2 A) |
| Temperature recorded and audited | `broker.normalized` sets it in every body (controller re-queries go through the same `_call`); `transport.json changes.temperature`; `invocation.json`; `freeze.settings.{temperature, broker_temperature}`; audit checks the freeze, each invocation and each `api/NNN/request.json`; resume under another temperature fails `verify_freeze`. `_TrackedHTTPS.https_open` is byte-for-byte the 3.12.14 `HTTPSHandler.https_open` except for the connection factory | holds |
| Metrics never print answer values | Read `render` / JSON contents (counts, rates, rule ids, task ids, ordinals). Official `score_answer` catches every exception and returns 0.0; it has no `print` | holds |
| Broker abandon | Read `abandon`, `__exit__`, handler `finally`; the tests cover a stall after the headers (plain and controller) and a stall before the headers | holds; the two stated gaps remain (TCP connect, HTTP error body) |
| pilot-roots | Read `pilot_addendum` / `pilot_configs` / `pilot_roots`. Every config goes through `arms.parse_config` (neutral names, barrier, subset hash) | holds; creates nothing paid |

## 2. Defects fixed

**A. `full_audit.py` silently skipped the seal/grade coverage check** (≈ line 177).
- Before: `if seal_path.is_file() and metrics_path.is_file(): self.check('seal and grades cover exactly this root's questions', ...)`. With a missing `paired-metrics.json` or seal, the check was never emitted, so the audit could pass.
- Now: the check always runs, and a missing file fails it. A completed campaign always seals and grades (`Campaign.finalize`), so real roots are unaffected.
- Regression: `test_subset_campaign_learns_in_blocks_seals_grades_and_audits` moves `paired-metrics.json` away and asserts the check fails.

**B. `pilot/metrics.py` ledger coverage matched prose numbers** (`Episode.__init__`).
- Before: `linked_answer = self.final`, the whole final message. `links_to_ledger` accepts it when *any* number token links. The final message is free text, so a year or table number the model repeated would count as a linked answer. The pack itself forbids exactly this for gate candidates (`packs/officeqa/pack.py extract_final`, S4-review 1). Criterion 1 (coverage ≥ 60%) could pass on answers with no provenance.
- Now: `extract_final(self.final)` is used, the same span the gate checks; the submitted answer is the fallback, as before. Right/wrong grading still uses the full sealed text, exactly as the official grade does.
- The `ADDENDUM.md` coverage definition now says so.

**C. `pilot/metrics.py` recorded a grader/grade-file disagreement without acting on it.**
- Before, three cases went through without an error:
  - `grade_file_agrees: false` appeared only in the JSON, and `render` never showed it;
  - a missing `<arm>-grades.json` gave `None`;
  - roots with different subsets were pooled.
- Now `root_metrics` refuses a root:
  - with no grades file;
  - whose official strict grades differ from the controller-side grades of the sealed answers (id sets must match too).

  `compute` refuses roots that declare different subsets, and `render` prints the subset sha256.
- Regression: new `test_pilot_metrics_review_refusals_and_answer_span`.

Pinned hash updated: `pilot/ADDENDUM.md` `pilot-metrics-script-sha256: 367c239546a4712fb2ccdbe5df6648251ad565c05e16794f6d0cdaaed2cff0b6`. The value `ce5d6ee3…19dd` in `wave3/pilot-prep.md` §1.4 is stale.

## 3. Files changed by this review

| file | change |
|---|---|
| `full_audit.py` | seal/grade coverage check always runs (missing file = fail) |
| `pilot/metrics.py` | coverage on `extract_final` span; refuse missing / disagreeing grade file; refuse mixed subsets; subset sha in the table |
| `pilot/ADDENDUM.md` | metrics sha re-pinned; coverage definition wording |
| `tests/test_v9_pilot.py` | audit assertion added to the subset campaign test; new test `test_pilot_metrics_review_refusals_and_answer_span` |

Originals backed up in the session scratchpad (`scratchpad/orig/`).

## 4. Test results after the fixes

| command | result |
|---|---|
| `V9_SLOW=1 $PY -B -m pytest -q -p no:cacheprovider tests/test_v9_pilot.py` (before re-pinning) | 17 passed; the pin test failed as expected until the addendum was updated |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` | **408 passed, 0 failed** (323 s) |
| `$PY -B pilot/select_loop_prone.py --check` | `matches_file: true` |

No paid call; only 127.0.0.1; no key read; no reference answer or grader run on real data; no v8, frameworks-v8, MetaHarnessAdapter* or opus-officeqa-133 file modified; PREREG.md untouched; no pilot root created.

## 5. Remaining observations (not changed; low severity or a user decision)

1. **Enforce-note matching is by message text.** `shown_findings` treats a `final` check as shown when its message appears in any `enforce` Note. Two final checks with identical messages, only one re-queried, would both count as shown. This is rare, and it matters only for criteria 2 to 4 at the margin. The exact fix would match the controller's `enforce` decision records by round.
2. **Indirect label influence in the selection pool.** The rule is behaviour-only, as specified. But the v8 `cards` arm's behaviour was shaped by cards learned from released references. That is allowed by the spec ("any v8 arm"), but worth one sentence in the addendum if you want it airtight.
3. **Loop-prone selection correlates with difficulty.** Pilot accuracy and rates will not represent Pro-133. That is intended (PREREG §7), and the pilot is excluded from confirmatory analysis.
4. **Explicit `"subset": null`** in `.task-paths.json` is accepted as a full run. That is harmless, but it is not refused the way `"temperature": null` is.
5. **The budget cap is operator-enforced only** (README step 6), as the implementer said.
6. **Broker gaps:** a stall inside the TCP connect or while reading an HTTP error body is not cut. PROTOCOL §3 records this, and a handler still busy after 10 s is marked `transport_in_flight`.
