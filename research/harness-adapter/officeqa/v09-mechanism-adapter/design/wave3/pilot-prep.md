# v9 wave 3: pilot preparation (2026-09-28)

## 中文摘要（给用户）

**结论：** READINESS §6 列出的 4 块缺失代码已全部补上。推荐项（broker 退出时切断卡住的上游调用）也已完成。

在付费 pilot 之前，只剩你的 3 个决定。你把决定填进 `pilot/ADDENDUM.md`，再按 `pilot/README.md` 的命令运行即可。

**先解释几个词：**
- **subset（子集）**：只跑 133 题中声明过的一部分题（这里是 20 题）。子集写在每个 root 的 `.task-paths.json` 里，并带一个 **sha256 哈希**。
  - sha256 是一种"指纹"算法：列表改动一个字符，指纹就完全不同。
  - 所以数据准备、运行、封存（seal）、评分、preflight、审计（audit）这 6 处都能核对"跑的是不是同一份题单"。
- **temperature（温度）**：模型采样的随机程度。0 = 尽量每次选概率最高的词；更高 = 更随机。
  - v8 一直写死 0。现在它是一个可声明的 broker 选项（broker = 夹在 Codex 和 Fireworks 之间的本地中转程序）。
  - 这个值会发送到每一次上游请求，并记录在 freeze（冻结记录）、invocation（每题启动记录）和 transport（每次调用记录）里。
  - 审计会逐条核对它。
- **label-free（不看标签）**：选题只看 v8 的"行为"，即同一条命令是否重复了 ≥3 次，不看答案、分数或对错。
- **go/no-go**：pilot 结束后，按 PREREG §7 的 5 条门槛判断能不能进入正式实验。
- **root**：一个独立的运行目录，是 plumbing（代码）的一份副本，外加自己的配置。

**做了什么（6 项）：**
1. 子集运行。
   - 数据准备仍是完整 133 题，所有哈希和 base 顺序都不变；manifest 额外记录子集。
   - 题目顺序 = 该 repeat 的随机顺序去掉其他题；仍按每 5 题一个 block，学习也照常工作。
   - 不声明子集时，行为与原来完全一样。
2. 温度。
   - broker 的 `temperature` 选项由 `.task-paths.json` 的 `temperature` 提供，默认 0（与 v8 相同）。
   - 如果 broker 实际发送的值与声明不符，campaign 拒绝启动。
3. 选题脚本 `pilot/select_loop_prone.py`。
   - 规则：v8 三个 arm（none、prompt、cards）中，任一 arm 的那次运行把同一条命令发了 ≥3 次，这道题就"会循环"。
   - 排序：先按"几个 arm 循环了"从多到少，再按固定哈希排序，取前 20。
   - 结果：133 题中有 46 题符合条件（3 个 arm 都循环 5 题，2 个 arm 20 题，1 个 arm 21 题）。
   - 选中的是：3 个 arm 都循环的 5 题，加上 2 个 arm 循环的 20 题中按哈希排在前面的 15 题。
   - 题单哈希：`81d2bf20c9f12033965abd728f71960d31a20e291ae3921bacba0f4da844dc56`。
   - 其中 4 题属于 issue #47（有争议的参考答案），照常计分。
4. go/no-go 指标 `pilot/metrics.py`。
   - 只在封存之后运行，用官方评分器在 controller 一侧给"出现 finding 之前的候选答案"打分。
   - 永远不打印任何答案值。
5. broker 退出时切断卡住的上游调用。
   - 响应头之前卡住、之后卡住，两种情况都能切断。
   - 调用记录在该题的 summary 生成之前就写完，之后不会再写任何记录。
6. `run.py pilot-roots`。
   - 零成本：在 plumbing 旁边创建 3 个中性命名的 root（`v9-p1-a`、`v9-p1-b`、`v9-p1-c`），不做任何付费调用。
   - 如果 PREREG.md 里还没有填好的附录（addendum），它会拒绝创建。

**测试结果：**
- 默认档：346 passed、61 skipped（opt-in 的 slow/live 测试）。
- 全开档：**407 passed，0 skipped，0 failed**，321 s（原 390 个 + 17 个新测试）。
- 我还在 scratchpad 里模拟了 pilot root 的布局，并在 root 内部跑了整套测试，见 §5。

**你需要决定（写进 `pilot/ADDENDUM.md` 的 3 个 USER 字段）：**
1. 温度格的具体数值，即 GLM-5.3-flash 的 model-card temperature。
2. 预算上限（美元，3 个 root 合计）。
3. 正式实验温度的选择规则。必须在 pilot 之前写下，否则 pilot 结果会影响规则本身。我给了一个建议写法。

另外请你复核 `ADDENDUM.md` 里 go/no-go 指标的"操作性定义"（§4）。PREREG §7 没有写细，这些定义是我定的。

**安全边界：**
- 没有任何付费调用。网络只连了 127.0.0.1（本机）。
- 没有读取任何密钥。
- 没有在真实数据上打开参考答案或评分器；也没有运行 `data.py prepare`（它会读取官方答案表，要由你运行）。
- 没有修改 v8 目录、frameworks-v8、MetaHarnessAdapter*、opus-officeqa-133。
- 没有创建真实的 pilot root。

---

Paths are relative to `officeqa-pro133-v9-plumbing/` unless absolute.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## 1. What was built (the six items)

### 1.1 Subset runs (READINESS §6 blocker 1)

- **Declaration.** `.task-paths.json` `"subset": {"name", "task_ids", "sha256"}`, parsed into
  `arms.SubsetSpec`. `sha256 = arms.subset_sha256(task_ids)` = sha256 of `json.dumps(task_ids,
  separators=(',', ':'))`. The parser refuses:
  - a wrong hash;
  - duplicate ids;
  - an empty list;
  - an unknown key;
  - a bad name.
- **Preparation.** `data.py prepare` still prepares all 133 questions, so every hash, the base order and
  the issue-47 list are unchanged. `manifest_v9` adds `"subset"` and sets `grading_policy` denominators to
  the subset.
  - `data.check_subset` / `verify_prepared` refuse a manifest whose subset differs from the spec, or whose
    ids are not prepared.
  - `data.selected_task_ids(manifest)` re-checks the hash on every use.
- **Order.** `arms.campaign_order(base, spec)` = `question_order(selected ids, order_seed, repeat)`. That
  equals the full repeat-r order with the other questions removed (tested). Blocks of 5: the 20-question
  pilot has 4 blocks. The barrier registers the subset order.
- **Seal / grade.** `seal_predictions` requires exactly the subset and records `seal.subset {name, sha256,
  count}`. `grade` requires the seal's subset hash to equal the manifest's, and grades only the subset:
  `denominator` = its size, undisputed = the subset minus issue-47 ids. `paired-metrics.json` records the
  subset. `release_references` is limited to the selected ids.
- **Campaign.** `load` refuses a spec/manifest subset mismatch, including when `verify_data` is off in
  tests. `state.json` records `question_count` and `subset`. `report.html` shows "N questions (subset
  name)".
- **Learning on subsets** works unchanged. Tested with `text-evolve` on a 7-question subset: 2 blocks, a
  learning round after block 1, issue-47 withheld, verification on released questions only, seal, grade
  and audit pass.
- **Preflight.** `question_count_133` still holds (the prepared set). New outputs:
  - check `subset_hash_matches_manifest`;
  - `info.question_count` and `info.subset`;
  - the order and the first offline workspace come from the subset order;
  - readiness records `question_count`, `subset` and `temperature`.
- **full_audit.** The order check uses the subset order. New checks:
  - `subset: hash of the declared ids, recorded in the prepared manifest, run exactly`;
  - `seal and grades cover exactly this root's questions` (runs for full roots too).

  When there is no `.task-paths.json`, `spec()` rebuilds `subset` / `temperature` from the frozen record.
- **A full run is unchanged.**
  - `CampaignSpec.to_json()` omits `subset` and a default temperature, so a full root's frozen spec has
    exactly the pre-wave-3 keys (tested).
  - No `subset` key appears in the manifest, seal, metrics or state.
  - Two record-level additions apply to every root: `invocation.json` gains `temperature` (§1.2), and the
    broker's urllib handlers are the tracked equivalents (§1.5).

### 1.2 Temperature (blocker 2)

- `.task-paths.json` `"temperature"`: a number in [0, 2], parsed by `arms.parse_temperature`. An explicit
  `null`, a bool or a string is refused. Absent means `arms.DEFAULT_TEMPERATURE` = 0, the v8 value.
  `experiment.TEMPERATURE` = the root's declared value.
- `broker.normalized(..., temperature=)` and the `Broker(temperature=)` option: the value is sent in every
  upstream body, including controller re-queries (tested with a real re-query). `transport.json
  changes.temperature` records it. The broker refuses values outside [0, 2].
- The campaign passes `spec.temperature` to every episode:
  - scored and verification episodes;
  - `Hooks.episode` receives `temperature=` too;
  - `run.py smoke` uses the root's declared value.

  `acting.run_actor(temperature=)` records it in `invocation.json`.
- Freeze: `settings_record(temperature)` gives `temperature` and `broker_temperature`; `campaign` includes
  `temperature` when it is not 0. `Campaign.load` refuses when the broker would send something other than
  the declared value. The old `experiment.TEMPERATURE != broker` check is replaced; its test now patches a
  broker that ignores the option. A resume under another temperature fails `verify_freeze`.
- Audit: `declared temperature = frozen = what the broker sends`, `<episode> invocation temperature as
  frozen`, and the existing `api/NNN temperature as frozen` on every request.

### 1.3 Label-free question list (blocker 4): `pilot/select_loop_prone.py` → `pilot/loop_prone_20.json`

- **Rule.** A scored v8 run *looped* when it issued one identical command ≥ 3 times:
  - "identical" = the whitespace-normalised `command` of completed `command_execution` items;
  - the normaliser is `harness_core.ledger.normalize_command`, the one the loop guard uses.

  Eligible questions loop in ≥ 1 of the v8 arms `none`, `prompt`, `cards`. Ranking: the number of arms
  that looped (desc), then `sha256("v9-officeqa-pilot-loop-prone" NUL task_id)` (asc). The first 20 are
  taken, and the ids are stored in ascending order.
- **Reads only** `runs/<arm>/NNN/task.json` (task_id, ordinal) and `records/events.jsonl` (command
  strings). The test builds a synthetic v8 tree whose prediction, summary, final, grades, answers and
  `index.json` files are mode 000. An audit hook records every file the script opens: only `task.json`
  and `events.jsonl`.
- **Result.**
  - eligible: 46 of 133;
  - by number of v8 arms that looped: 3 arms → 5 questions, 2 arms → 20, 1 arm → 21;
  - the 20: 5 three-arm + 15 of the 20 two-arm questions;
  - 4 are issue-47 ids: UID0004, UID0093, UID0154, UID0238.
- **Ids:** UID0004 UID0028 UID0030 UID0037 UID0039 UID0093 UID0114 UID0121 UID0122 UID0150 UID0154
  UID0165 UID0172 UID0183 UID0188 UID0205 UID0214 UID0220 UID0238 UID0245.
- **Hashes:**
  - list: `81d2bf20c9f12033965abd728f71960d31a20e291ae3921bacba0f4da844dc56`;
  - file: `3c8636cdf8d3efaadbc5185853441f65a5e05bf7eb15a67423b4c56f59094aa6`;
  - script: `0a810f09732735c628e52db670e642396a454327fae114e6afd36793bc0c5cc6`.
- `--check` re-derives the list from the v8 traces. A test does the same and passes.

### 1.4 Go/no-go metrics (blocker 3): `pilot/metrics.py` (sha256 `ce5d6ee3…19dd`)

- It refuses a root that is not `completed` and sealed (`private-evaluation/predictions-seal.json`). It
  reads references from `prepared/private/answers.jsonl` only after that check, and checks the file's sha
  against the manifest.
- Grading: each root's pinned `reward.py`, strict tolerance 0, via `data.official_scorer`. The grader is
  injectable; tests use a toy exact-match grader. It cross-checks the final grades against
  `<arm>-grades.json` (`grade_file_agrees`).
- Numbers (mech-static, pooled if several roots):
  1. ledger coverage;
  2. finding rate on right vs wrong episodes;
  3. wrong→right and right→wrong flips after a finding;
  4. union fire rate on right episodes, and right→wrong as a share of fired-right episodes;
  5. submit rate and re-query rate.

  Also reported:
  - loop notes, and whether a noted command was issued again after its note;
  - per rule: first shown on right/wrong, and engine fires on right/wrong candidates, lowest precision
    first (the list for "disable the lowest-precision rule").

  For every root: accuracy, empties, statuses, loop runs, recovered and right.
- Thresholds are copied from PREREG §7/§5: 0.60, 2×, 3×, 0.20, 0.05; submit rate < 50% is flagged
  INVESTIGATE. "NO DATA" never counts as a pass.
- Output: a text table plus an optional JSON. Neither holds any answer value (asserted in tests over the
  table, the JSON and the command-line output).
  Exit code: 0 GO, 1 NO-GO, 2 no mech-static root given.
- **Definitions I had to choose** (PREREG §7 does not spell them out; written into `pilot/ADDENDUM.md` for
  you to confirm):
  - "shown finding": a `submit` check with findings, or an enforcement check whose re-query was actually
    sent (its `enforce` Note is in the ledger);
  - right/wrong for criteria 2 and 4: the grade of the *judged candidate*. That is the pre-finding
    candidate if the episode fired, otherwise the last checked candidate, otherwise the final answer. The
    rates by final-answer grade are reported next to them;
  - coverage denominator: answered episodes. The all-episode rate is reported too.

### 1.5 Broker: abandon the upstream call on exit (READINESS §6 recommendation; S8 request 4)

- On exit, `Broker.abandon()` sets stop and shuts down the socket of the call in flight. This covers
  both cases:
  - before the response headers, via the connection held by `_TrackedHTTP` / `_TrackedHTTPS`;
  - after the headers (the stalled stream), via `active_response`.

  The broker exits when the episode ends or the actor is killed, including on exceptions. It then waits
  up to `EXIT_WAIT_S` = 10 s for the handler.
- The handler records the call before `execute_episode` reads `api/summary.json`: a dispatched attempt
  with `usage_unknown`, then `not_dispatched` ("Transport stopped"). `api/summary.json` gains
  `abandoned_calls` (only when non-empty). Nothing is written afterwards and nothing is re-sent. Tested
  for all three cases (plain relay and controller relay after the headers; before the headers), with a
  byte comparison of `api/` after the fake provider finally closes.
- The e2e adversity rehearsal (real Codex) now asserts:
  - the stalled call is fully recorded before the seal and audit;
  - `abandoned_calls == [n]` and `in_flight_calls == []`;
  - `api/` is byte-identical after the provider closes.

  Previously it asserted the opposite (late record).
- **Production-path change to know about.** `build_opener` now receives urllib's HTTP/HTTPS handlers
  subclassed only to expose the connection. They use the same default SSL context and the same
  `do_open`.
- **Still out of reach:** a stall inside the TCP connect, or while reading an HTTP error body. A handler
  busy > 10 s is marked `transport_in_flight` as before. This is in PROTOCOL §3.

### 1.6 `run.py pilot-roots` + `pilot/README.md`

- `run.py pilot-roots [--parent DIR]`, $0: copies this plumbing next to itself as the three roots below
  and writes each root's `.task-paths.json`.

  | root | arm | temperature |
  |---|---|---|
  | `v9-p1-a` | `text-static` | 0 |
  | `v9-p1-b` | `mech-static` | 0 |
  | `v9-p1-c` | `text-static` | cell |

  Each copy excludes caches, `preflight/`, `prepared/` and runs. Each `.task-paths.json` holds:
  - profile v9, arm, repeat 1;
  - `order_seed` `v9-officeqa-pro133-order`;
  - `temperature`;
  - `subset`;
  - barrier `v9-p1-barrier` over the three roots, 43,200 s;
  - `framework` / `data_cache` (the v8 values; they must exist).

  Every config is validated with `arms.parse_config`.
- It refuses:
  - inside a root;
  - an existing root or barrier dir;
  - a PREREG.md without the filled addendum: exactly one `pilot-list-sha256: <this list's hash>`, one
    numeric `pilot-cell-temperature` ≠ 0, no `[USER:` left, and the PREREG order seed present.

  It runs no data preparation and no model call.
- `pilot/README.md` has the exact commands (§3 below). `pilot/ADDENDUM.md` is the addendum text with the
  list, file and script hashes pinned (a test keeps them current) and the three USER fields.

## 2. Files changed / added

| file | change |
|---|---|
| `arms.py` | `SubsetSpec`, `subset_sha256`, `parse_subset`, `parse_temperature`, `selected_ids`, `campaign_order`; `CampaignSpec.subset/temperature`; `to_json` omits defaults; `.task-paths.json` keys `subset`, `temperature` |
| `experiment.py` | `TEMPERATURE` = the root's declared value (default 0) |
| `broker.py` | `temperature` option (normalized, records); `_TrackedHTTP/_TrackedHTTPS`, `_shutdown`, `Broker.abandon`, `EXIT_WAIT_S`, `abandoned_calls` in summary |
| `acting.py` | `run_actor(temperature=)` recorded in invocation.json |
| `campaign.py` | subset order and task filter; `data.check_subset` when not verifying; temperature to every episode and hook; `broker_temperature(t)`, `settings_record(t)`; state `subset` / `temperature`; docstring |
| `data.py` | `subset_json`, `selected_task_ids`, `check_subset`; manifest subset and denominators; verify, seal, grade, release on the subset; `_spec_from_manifest` subset |
| `preflight.py` | subset order and check, temperature check vs spec, readiness fields; suite `orchestration` also runs `tests/test_v9_pilot.py` |
| `full_audit.py` | subset/seal/grade checks, temperature freeze and invocation checks, spec rebuild with subset/temperature |
| `report.py` | h1 shows the question count and subset name |
| `run.py` | smoke uses the root temperature; `pilot-roots` command (`pilot_addendum`, `pilot_configs`, `pilot_roots`) |
| `PROTOCOL.md` | §1 keys, §2 L0 item 8 temperature, §3 stalled calls (closed), §4 subset metrics |
| `tests/test_v9_orchestration.py` | fakes accept/forward `temperature`; temperature constant and broker-mismatch tests updated |
| `tests/test_v9_e2e.py` | episode hook forwards `temperature`; adversity test asserts the stalled call is recorded before the audit and never after |
| new: `pilot/__init__.py`, `pilot/select_loop_prone.py`, `pilot/loop_prone_20.json`, `pilot/metrics.py`, `pilot/ADDENDUM.md`, `pilot/README.md` | |
| new: `tests/test_v9_pilot.py` | 17 tests (subset, temperature, preflight subset root, broker abandon ×4, selection, list re-derivation + addendum pins, metrics ×3, pilot-roots) |

PREREG.md was **not** edited. The addendum is appended by you after filling it.

## 3. Commands for you (full version in `pilot/README.md`)

```
cd /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
$PY -B pilot/select_loop_prone.py --check                       # $0: list re-derives (matches_file: true)
# edit pilot/ADDENDUM.md (3 USER fields), then:
cat pilot/ADDENDUM.md >> PREREG.md
V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests    # $0, ~5.5 min
$PY -B run.py pilot-roots                                       # $0: ../v9-p1-{a,b,c}
for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && $PY -B data.py prepare && $PY -B preflight.py --offline-only) || break; done
# paid, with your approval and FIREWORKS_API_KEY:
for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && $PY -B preflight.py) || break; done      # smoke, cents each
for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &); done
# after all three completed:
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p1-b ../v9-p1-c --json ../v9-p1-metrics.json
```

## 4. Test results (exact commands, from the plumbing)

| command | result |
|---|---|
| `$PY -B -m pytest -q -p no:cacheprovider tests` (final code) | **346 passed, 61 skipped** (opt-in groups), 104 s |
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` (final code) | **407 passed, 0 skipped, 0 failed**, 321 s (390 before + 17 new) |
| `preflight.pytest(SUITES['orchestration'])` after adding `test_v9_pilot.py` to it | passed (55 passed, 2 skipped), in the plumbing and inside the simulated `v9-p1-c` (with `broker` and `core` suites also passing there) |
| `$PY -B -m pytest -q -p no:cacheprovider tests/test_v9_pilot.py -k "abandon or exit"` ×6 | 4 passed each time (flake check of the socket tests) |
| `$PY -B pilot/select_loop_prone.py` | wrote the list (46 eligible), sha `81d2bf20…dc56`; a second run gives the same sha |

## 5. Simulated pilot roots (scratch, $0)

Goal: check what breaks when `preflight.py` runs the suites *inside* a pilot root, which has a
`.task-paths.json` with a subset and (for `v9-p1-c`) temperature 0.6.

**Layout.** In `scratchpad/sim/`:
- symlinks to the read-only siblings (frameworks-v8/v9, the three v8 roots, v8 plumbing,
  adapter-comparison-work, opus-officeqa-133, baseline-work);
- a copy of the plumbing with a filled addendum appended to PREREG.md, then `run.py pilot-roots` → three
  roots;
- the Codex / frozen-runtime paths set explicitly, because Seatbelt refuses to exec Codex through a
  symlinked directory (the READINESS §2 artefact).

**Control.** A full-run root `v9-z1-a` (no subset, no temperature) in the same scratch layout.

| where | result |
|---|---|
| inside `v9-p1-c` (text-static, subset, T=0.6), all tiers | 399 passed, 8 failed |
| inside `v9-p1-b` (mech-static, subset, T=0), all tiers | 399 passed, 8 failed |
| inside the control `v9-z1-a`, all tiers | 399 passed, **the same 8** failed |

What the 8 failures were:
- **7 layout artefacts** in `tests/test_v9_kit.py`: `test_probe_kit_sh[3 arms]`,
  `test_real_codex_exec_in_v9_sandbox`, `test_v8_v9_differences_are_the_documented_ones[3]`. They fail
  identically in the control root and pass in the plumbing: the kit and Seatbelt grants need real, not
  symlinked, framework paths. Real pilot roots sit next to the real frameworks, like the plumbing.
- **1 real bug in my own test**, fixed: `test_pilot_roots_need_the_filled_addendum_then_create_three_roots`
  assumed PREREG.md had no pilot addendum yet. That stops being true once you append it. The test now
  strips the addendum from its private copy and builds its own placeholder case. After the fix,
  `tests/test_v9_pilot.py` passes: 17/17 inside `v9-p1-b`, `v9-p1-c`, `v9-z1-a` and the plumbing copy.

**Your workflow, reproduced end to end** (`scratchpad/sim_user_flow.py`):
- fill `pilot/ADDENDUM.md` in place;
- `cat >> PREREG.md`;
- `tests/test_v9_pilot.py` + `tests/test_v9_orchestration.py`: 55 passed, 2 skipped;
- `run.py pilot-roots --parent <fresh>` creates a (T 0), b (T 0), c (T 0.6).

`test_v9_pilot.py` is now part of preflight's `orchestration` suite, so every root's readiness re-runs the
subset, temperature, broker-abandon and metrics tests on its own code copy.

## 6. Open items and notes

- **Your decisions**, in `pilot/ADDENDUM.md`:
  - the cell temperature;
  - the budget cap;
  - the confirmatory-temperature rule;
  - confirming the metric definitions (§1.4).
- **Budget is not enforced by code.** The stop rule is operator-side (README step 6: add up
  `known_cost_lower_bound_usd`, SIGINT at the cap).
- **Operator steps nobody has run** (they read the official download):
  - `data.py prepare` in the pilot roots;
  - `preflight.py` on real prepared data.
- **Unchanged from READINESS:**
  - the learner transport is still unsmoked; the pilot does not need it;
  - `run.py run` does not refuse pending `[DECISION PENDING` lines in PREREG (S6 request 7).
- For every root, the copy includes `pilot/` (the list, the scripts, the addendum). `pilot/` is not
  part of the freeze's hashed trees. `metrics.py` is run from the plumbing, and its sha256 is pinned in
  the addendum.
- The pilot list contains 4 issue-47 questions. They are scored as usual. `metrics.py` reports all
  episodes; say if you want an undisputed-only view added.
