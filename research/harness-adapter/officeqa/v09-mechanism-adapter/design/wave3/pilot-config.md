# v9 wave 3: pilot configuration (2026-09-28)

## 中文摘要（给用户）

**结论：** 你的 3 个 pilot 决定已全部写进代码和预注册文件（PREREG）。完整测试 **419 passed、0 failed**（全开档，332 s）。没有任何付费调用。

**先解释几个词：**
- **top_p（核采样，nucleus sampling）**：模型每次选下一个词时，只在"累计概率达到 p 的那一小撮候选词"里抽样。
  - 例：top_p=0.95 表示把最不可能的 5% 尾巴砍掉，再按 temperature 抽样。
  - 它和 temperature（温度，随机程度）一起决定输出有多"随机"。
  - GLM-5.3-Flash 的模型卡（model card，官方说明页）在 agentic / 工具调用评测里用的是 temperature=1.0、top_p=0.95。
- **broker**：夹在 Codex 和 Fireworks 之间的本地中转程序。它负责把采样参数写进每一次发往上游的请求体（request body）。
- **cell（格）**：pilot 里的一个实验条件，对应一个独立的运行目录（root）。
- **loop run（循环运行）**：一道题的运行里，同一条（空白规范化后的）命令被执行了 ≥3 次。
- **strict accuracy（严格准确率）**：官方 `reward.py` 在容差 0 下判对的题数。
- **addendum（附录）**：PREREG 被哈希后，任何改动只能以"带日期的附录"追加在文末，不能改原文。

**做了什么（5 项）：**
1. **top_p 选项。**
   - `.task-paths.json` 新增可选键 `top_p`（取值 0 < p ≤ 1）。
   - 不写这个键 = 请求体里根本没有 top_p，与 v8 和改动前逐字节相同。这一点由现有的"与 v8 逐字节一致"测试保证。
   - 写了这个键：broker 在每一次上游请求里都发送它，包括 controller 的重问（re-query）。它会被记录在 `transport.json`（只在发送时）、`freeze.json`、`invocation.json`。
   - 审计（`full_audit.py`）逐条请求核对：声明了就必须等于声明值；没声明就必须完全没有 top_p 键。
   - broker 实际发送的值与声明不符时，campaign 拒绝启动。
   - 付费 smoke 现在也逐条核对请求体里的 temperature 和 top_p。
   - **学习器（learner）的模型调用不受影响**：`client.py` 仍用它自己的 temperature=0、top_k=40。top_p 只作用于 actor（打分题和验证题的 actor）。
2. **题单 = 前 50 题。**
   - `pilot/first50.json` 是标准题序的前 50 个 task id。标准题序 = v3–v8 manifest 的顺序，种子 `metaworld-officeqa-pro-codex70-online-v1`。
   - 题单指纹：`c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d`。
   - 我核对了四处：v8 manifest 的顺序、按种子重算的顺序、v8 三个 arm 的 ordinal、历史上 5 个 50 题 root（v4 gold、v5、v6f×2、v7 cards），它们完全一致。
   - 选题脚本只读 task id 和 ordinal，不读任何答案、成绩或轨迹。
   - 旧的 `loop_prone_20.json` 保留作记录，pilot 不用它。
3. **三个 cell。**
   - `v9-p1-a` 是 text-static，T=0；`v9-p1-b` 是 mech-static，T=0。这两个 cell 都不发送 top_p。
   - `v9-p1-c` 是 text-static，T=1.0，top_p=0.95。
   - 三个 cell 都用同一份 50 题、PREREG 的 order_seed，并共用一个 barrier。每个 cell 10 个 block。
4. **ADDENDUM 已填写并追加到 PREREG.md。**
   - 题单指纹、温度 1.0 + top_p 0.95（附来源）、预算"无上限；按 v8 价格估计约 $30–45"。
   - 正式实验的温度规则逐字写入：c 比 a 的 loop run 至少少 50%，**并且** c 的严格准确率比 a 低不超过 2 题（满分 50 题）→ 所有正式 arm 用 T=1.0、top_p 0.95；否则用 T=0。
   - `pilot/metrics.py` 现在计算这两个量（每个 cell 的 loop run 数、准确题数之差），并打印 `RULE VERDICT`（规则判定）。
   - go/no-go 的阈值与 PREREG §7 相同，没有改动。
5. **测试**：新增 11 个测试项（7 个测试函数，其中一个有 5 组参数），并扩充了 5 个已有测试，覆盖 top_p、前 50 题和温度规则。

**你要的计数（只有计数，没有答案）：**
- 50 题里有 **17 题**属于 loop-prone 候选集（133 题里共 46 题）。按"v8 里几个 arm 出现循环"分：3 个 arm 4 题，2 个 arm 9 题，1 个 arm 4 题；其余 33 题在 v8 里从未循环。
- 旧的 20 题 loop-prone 题单里有 12 题在这 50 题中。
- 50 题里有 5 题属于 issue #47（参考答案有争议），照常计分。
- 参考：这 50 题在 v8 里的 loop run 数分别是 none 9、prompt 12、cards 13。所以 cell a 大约会有 10 个左右，规则要求 c 不超过其一半。

**请你确认的 3 点：**
1. **边界情况**：如果 cell a 一个 loop run 都没有，我把"至少少 50%"判为"不满足"（没有东西可减），结果是 T=0。这一条已写进附录的操作性定义。
2. "恰好少 50%"算满足（c ≤ 0.5 × a）。"低 2 题"算满足（c − a ≥ −2）。
3. **注意**：go/no-go 是在 mech-static、T=0 下测出来的。如果规则选中 T=1.0，gate 的精确度在 T=1.0 下未经 pilot 检验。这是一个观察，我没有改任何东西。

**安全边界：**
- 没有付费调用。网络只连 127.0.0.1。没有读取密钥。
- 没有在真实数据上打开参考答案或评分器。
- 没有修改 v8 目录、frameworks-v8、MetaHarnessAdapter*、opus-officeqa-133。
- 没有创建真实的 pilot root。

---

Paths are relative to `officeqa-pro133-v9-plumbing/` unless absolute.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.
Originals of every edited file are in the session scratchpad (`scratchpad/orig-pilot-config/`).

## 1. The user's decisions, as implemented

| decision (verbatim) | where it lives now |
|---|---|
| cell temperature "1.0, top_p 0.95" | `pilot/ADDENDUM.md` → PREREG.md: `pilot-cell-temperature: 1.0`, `pilot-cell-top-p: 0.95` (source: the GLM-5.3-Flash model card's agentic / tool-use evaluation settings); `run.py pilot-roots` writes `"temperature": 1.0, "top_p": 0.95` into `v9-p1-c/.task-paths.json` only |
| pilot size "50 real questions × 3 groups, no budget cap" | `pilot/first50.json` (50 ids); `run.PILOT_LIST` points to it; `pilot-budget-cap-usd: no cap (user decision); estimate ≈ $30-45 at v8 prices` |
| confirmatory temperature rule "data decides" | rule text verbatim in the addendum; `pilot/metrics.py` computes it and prints `RULE VERDICT` |

Sanity check of the estimate: in v8, actor cost per question was $0.19 (none), $0.21 (prompt) and $0.24 (cards) (`known_cost_lower_bound_usd` / 133). 150 episodes therefore come to about $28–36 as a lower bound, which fits "$30–45".

## 2. Changes, file by file

### 2.1 top_p (task item 1)

| file | change |
|---|---|
| `arms.py` | `CAMPAIGN_KEYS` += `top_p`; `DEFAULT_TOP_P = None`; `parse_top_p` (number in (0, 1]; bool, null, string, 0, NaN refused; leave the key out to send none); `CampaignSpec.top_p`; `to_json` omits it when None, so a full-run spec is unchanged; `parse_config` reads it; docstring |
| `broker.py` | `normalized(..., top_p=None)`: None leaves the body exactly as before (Codex sends no top_p); a number sets `top_p`. `Broker(top_p=None)` is validated. `_call` records `changes.top_p` **only when sent**, so the controller=None records stay byte-identical to v8 (`test_v9_broker::test_1`). The handler passes `broker.top_p`. The controller re-query copies the normalised body (`revised = dict(outgoing)`), so it carries top_p too |
| `acting.py` | `run_actor(top_p=None)` records `invocation.json` `"top_p"` (null = not sent); comment |
| `campaign.py` | `execute_episode(top_p=)` passes the Broker option and records it; `broker_top_p()`; `settings_record(temperature, top_p)` adds `top_p` and `broker_top_p` (null when not declared) to `freeze.json`; `freeze_record` / `verify_freeze` use them (a resume under another top_p is refused); `Campaign.load` **refuses when the broker would send a different top_p**; `state.json` records `top_p` when declared; `run_one` passes `top_p` to scored and verification episodes and to `Hooks.episode`; docstrings |
| `experiment.py` | `TOP_P = CAMPAIGN.top_p` (default None) |
| `full_audit.py` | the spec is rebuilt with `top_p`; `v9 settings as frozen` includes it; new checks `declared top_p = frozen = what the broker sends (null = not sent)`, `<episode> invocation top_p as frozen` and, **per request**, `<episode> api/NNN top_p as frozen` (a declared top_p must be in every body; an undeclared one must be absent from every body) |
| `preflight.py` | check `top_p_sent_equals_declared`; `info.top_p`; `readiness.json` `top_p` |
| `run.py` | smoke: sends the root's top_p, records it, and a new `sampling_ok` (every request body has the declared temperature, and top_p exactly as declared, i.e. absent when undeclared) is part of `passed` |
| `PROTOCOL.md` | §1 key `top_p`; §2 L0 item 8 "Temperature and top_p" (records, audit, learners unaffected) |

**Learners are unaffected.** `top_p` reaches only actor episodes: scored episodes, plus verification actors in evolve arms, which go through `Campaign.run_one`. The learner model calls (`client.py`: `temperature=0, top_k=40`) are unchanged. The pilot has no learner.

### 2.2 Pilot list: first 50 (task item 2)

- **New `pilot/select_first50.py`.**
  - Base order: the v8 none root's `prepared/manifest.json` (it reads only `task_ids`, `selection.seed` and `sample_count`; the manifest holds the answer file's hash, not answers).
  - It checks that this order equals the recomputed order, ascending sha256(seed NUL uid) (`officeqa_prepare.select_rows`' key, seed = `data.SEED`), and the ordinals in the three v8 arms' `task.json`.
  - It takes the first 50.
  - Cross-check: every historic 50-question root on disk ran exactly these 50 at ordinals 1–50: `v4-gold`, `v5-mwm`, `v6f-mwm`, `v6f-prompt`, `v7-cards`. (v6c was partial and is not in the list.)
  - `--overlap` prints the loop-prone counts. Only this option reads v8 command strings; the list does not depend on it.
- **New `pilot/first50.json`**: `{name: pilot-first50, task_ids (ascending), ordinals (base-order positions 1–50, aligned), sha256, count, rule, questions_in_base_order, historic_50_matches, inputs, script_sha256}`.
  - The ids are stored in ascending order, like `loop_prone_20.json`. The campaign still runs them in the repeat-1 seeded order.
- `run.py`: `PILOT_LIST_FILE = 'pilot/first50.json'`.
- `loop_prone_20.json` and `select_loop_prone.py` are kept unchanged, for the record. `select_loop_prone.py --check` still gives `matches_file: true`.

**Counts (no answers):**

| quantity | value |
|---|---|
| first-50 questions in the loop-prone eligible set | **17 of 50** (eligible: 46 of 133) |
| by the number of v8 arms that looped | 3 arms: 4, 2 arms: 9, 1 arm: 4, never looped: 33 |
| questions of the old loop-prone 20 among the 50 | 12 |
| issue-47 items among the 50 | 5 |
| v8 loop runs within these 50 (none / prompt / cards) | 9 / 12 / 13 |

### 2.3 Three cells (task item 3)

`run.PILOT_ROOTS = (('v9-p1-a', 'text-static', 0, None), ('v9-p1-b', 'mech-static', 0, None), ('v9-p1-c', 'text-static', 'cell', 'cell'))`.

- `pilot_addendum` now also requires exactly one numeric `pilot-cell-top-p` in (0, 1].
- It keeps the cell temperature as written: `1.0` stays `1.0`. It used to be turned into the int `1`; the value is numerically the same.
- `pilot_configs` writes `top_p` only for c. The a and b configs have no `top_p` key, so they send none.
- All three share the subset `pilot-first50`, repeat 1, `order_seed v9-officeqa-pro133-order` and one barrier `v9-p1-barrier` (43,200 s). That gives 10 blocks of 5.
- Checked offline against the real PREREG.md: `pilot_addendum` returns `cell_temperature 1.0, cell_top_p 0.95`, and the three configs parse (a/b `top_p` None, c 0.95, 50 ids each).

### 2.4 Addendum, metrics, README, PREREG (task item 4)

- **`pilot/ADDENDUM.md`**, fully filled with no USER field left:
  - the questions section, rewritten for first50, with the counts above;
  - the pins;
  - the cells table with the top_p column;
  - temperature 1.0 and top_p 0.95 with their source;
  - the budget line;
  - the confirmatory rule, verbatim;
  - operational definitions of the rule (below);
  - the go/no-go criteria and their definitions, unchanged (the loop-run bullet now points to the rule's definition).
- **Operational definitions of the rule** (written in the addendum; please confirm):
  - loop run: at least 3 completed `command_execution` items with the same `normalize_command` string (the same rule as the selection and the existing metrics);
  - "at least 50% fewer": `loops_c ≤ 0.5 × loops_a`. **When cell a has no loop run, the condition is not met, so T=0.**
  - accuracy: the official strict grade (tolerance 0) of the sealed final answers; the condition is `right_c − right_a ≥ −2`;
  - "T=0" means temperature 0 with no top_p;
  - the rule is evaluated only on completed, sealed and graded roots, and its result goes into a further dated addendum before the first confirmatory call.
- **`pilot/metrics.py`** (new sha `dc302abc…03ba`):
  - `RULE_*` constants;
  - `temperature_rule(per_root)`: cell a is found as text-static with frozen T=0 and top_p null; cell c as text-static with T=1.0 and top_p 0.95. Without exactly one of each, the rule is "not computable". Different question counts are refused.
  - The rule's outputs: `loop_runs_a/c`, `loop_reduction`, `loop_condition_met`, `right_a/c`, `accuracy_difference_questions`, `accuracy_condition_met`, `confirmatory_temperature/top_p` and `verdict`.
  - The per-root table gains a top_p column; the rule block ends with `RULE VERDICT: confirmatory arms at T=1.0, top_p 0.95 | T=0`.
  - The go/no-go thresholds (0.60, 2×, 3×, 0.20, 0.05, submit < 50% INVESTIGATE) and the exit codes are unchanged.
- **`pilot/README.md`**: rewritten for first50, top_p, no cap, and the rule verdict in step 7.
- **`PREREG.md`**: the addendum is appended below the Addenda line (`cat pilot/ADDENDUM.md >> PREREG.md`). Lines 1–137 are byte-identical to before. The hash went from `7a7824a4…b939` to **`baa53c64c1c57ff499afc0955aa16a5aa21af6c8e4a5c5f9219bf975cae32fa3`**.
  - No readiness or freeze existed yet, so no recorded hash is stale.
  - The old values in `wave3/pilot-prep*.md` are historical.
- The six `[DECISION PENDING` lines of the hashed body remain, including §5 temperature. By design they are never edited; the addendum supersedes the pilot parts. `preflight.py` still lists them in `prereg_pending_decisions`.

**Pinned hashes (addendum = PREREG):**

| pin | sha256 |
|---|---|
| `pilot-list-sha256` (`arms.subset_sha256` of the 50 ids) | `c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d` |
| `pilot-list-file-sha256` (`pilot/first50.json`) | `00ff0832c67ff09ae648283b563781c07984deb2bf8c1bf402dbe4a548838a3d` |
| `pilot-selection-script-sha256` (`pilot/select_first50.py`) | `318ab73cdf8afdfd03aa007c87e23e68cee494bdc64a8e57660dafba4fe43d81` |
| `pilot-metrics-script-sha256` (`pilot/metrics.py`) | `dc302abc268c063c3f916b51f315a88a5c6d48eda82a1c03efc2f0fcf6fd03ba` |
| PREREG.md | `baa53c64c1c57ff499afc0955aa16a5aa21af6c8e4a5c5f9219bf975cae32fa3` |

### 2.5 Tests (task item 5)

**New tests in `tests/test_v9_pilot.py`:**

| test | what it checks |
|---|---|
| `test_top_p_parses_records_only_when_declared` | parse, `to_json`, refusals |
| `test_real_broker_sends_top_p_only_when_declared` | real broker + fake upstream: the body and `transport.json` carry top_p, the controller re-query carries it, the default sends none and records none, bad values are refused |
| `test_top_p_campaign_freezes_records_audits_and_refuses_a_mismatch` | freeze, state, invocation and request carry it; the audit passes; resume under another or no top_p is refused; tampering with an invocation or a request fails the named audit checks; a root without top_p has frozen null, and a body carrying top_p fails the audit; a broker that ignores top_p makes `load` refuse with `top_p 0.95 but the broker sends None` |
| `test_select_first50_reads_only_task_ids_and_takes_the_seeded_prefix` | synthetic v8 + historic tree with answer, final, summary, events and grades files at mode 000; an audit hook shows that only `task.json` and `manifest.json` were opened; the result equals `officeqa_prepare.select_rows(count=50)`; refusals for a historic mismatch, disagreeing ordinals and a non-seeded manifest |
| `test_committed_first50_list_is_pinned_by_the_addendum_and_re_derives_from_v8` | the pins; the addendum is in PREREG.md below "Addenda"; `run.pilot_addendum` on the real PREREG; re-derivation from the real v8 tree; the stated overlap count |
| `test_confirmatory_temperature_rule_on_synthetic_cells` ×5 | exactly 50% fewer with −2 → switch; 60% fewer with the same accuracy → switch; 40% fewer → T=0; 0 loops but −3 → T=0; a = 0 loops → T=0; the go/no-go stays NO-GO on the same mech episodes; no answer value in the output |
| `test_temperature_rule_needs_both_cells_on_the_same_questions` | not computable without a top_p cell; different question counts are refused; the CLI prints the verdict with exit 2 when there is no mech root |

**Adjusted tests:**
- In `test_v9_pilot.py`:
  - the preflight subset test now uses T=1.0 / top_p 0.95 and asserts `top_p_sent_equals_declared`;
  - the loop-prone committed-list test keeps the re-derivation, and its addendum pins moved to first50;
  - the pilot-roots test covers first50, a/b without a `top_p` key, c with 1.0 / 0.95, `experiment.TOP_P`, and refusal on a missing or invalid `pilot-cell-top-p`;
  - `make_sealed_root(top_p=)`;
  - the metrics test asserts "not computable" for a 0.6 cell.
- In `test_v9_orchestration.py`:
  - the fake broker, actor and episode forward `top_p`;
  - the live smoke test (real Codex, fake upstream, $0) asserts `sampling_ok` and runs a second smoke as a cell-c root. Every body of that smoke carries `(1.0, 0.95)`.
- In `test_v9_e2e.py`, the episode hook forwards `top_p`.

**Results:**

| command | result |
|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` (final code) | **419 passed, 0 failed, 0 skipped** (332 s); before: 408 |
| `$PY -B -m pytest -q -p no:cacheprovider tests` (default tier) | 358 passed, 61 skipped (opt-in tiers) |
| `$PY -B pilot/select_first50.py --check` | `matches_file: true`, historic matches: all 5 |
| `$PY -B pilot/select_loop_prone.py --check` | `matches_file: true` (unchanged draft) |

## 3. Notes and open items

1. **Please confirm the two operational choices of the rule:**
   - a = 0 loop runs → T=0;
   - "at least 50%" includes exactly 50%, and "not lower by more than 2" includes exactly −2.
2. **Go/no-go temperature vs the confirmatory temperature.** The go/no-go gate numbers come from mech-static at T=0 (cell b). If the rule selects T=1.0, the gate's precision at T=1.0 has not been piloted. This is a design observation only; nothing was changed.
3. **First paid top_p call.** The plumbing's `preflight/smoke-01` (5/5) ran before these edits: `broker.py`, `campaign.py`, `acting.py` and `run.py` have changed since. The per-root smoke (README step 5) runs the new code. The `v9-p1-c` smoke is the first call that sends `top_p: 0.95` to Fireworks. If the provider rejects it, that smoke fails before any pilot money is spent.
4. **Budget:** no cap by your decision. Cost is only read and reported per block (README step 6).
5. **Power of the rule:** the v8 arms had 9–13 loop runs within these 50 questions. Cell a will probably have about 10, so c needs about 5 or fewer.
6. The pilot runs in the repeat-1 seeded order of the 50 questions (the full repeat-1 order with the other 83 removed), not in the base order 1–50.

No paid call; only 127.0.0.1; no key or credential file read; no reference answer or grader opened on real data (the only answer-adjacent file read was the v8 manifest, for its `task_ids` / `selection.seed` / `sample_count`); no v8 root, frameworks-v8, MetaHarnessAdapter* or opus-officeqa-133 file modified; no pilot root created.
