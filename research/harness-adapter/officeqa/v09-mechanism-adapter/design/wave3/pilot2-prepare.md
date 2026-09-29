# v9 wave 3: re-pilot b2 root prepared ($0), 2026-09-29

## 中文摘要（给用户）

**结论：** 重跑 b 用的目录 `v9-p2-a` 已经建好，数据已准备好，**全部免费检查都通过了**（27/27 项，root 里的测试 387 passed、0 failed）。
这一步**没有任何付费调用**，没有读取或打印密钥，也没有打开 `prepared/private/` 里的文件（只看了文件权限）。
四个 pilot-1 目录、`v9-p1-metrics.json` 和 plumbing 在操作前后的文件列表、大小、修改时间完全一致。

**现在只差两步，都要付费，都需要你的 Fireworks 密钥：**
1. 付费就绪测试 `preflight.py`：几美分，约 5 分钟。pilot-1 b 的这一步花了 $0.002。
2. 正式运行 `run.py run`：pilot-1 b 的已知下限约 **$5.01**，b2 预计差不多。
   - 预计 **4–5 小时**。pilot-1 b 的 50 题实际答题时间合计 4.1 小时。
   - b2 只有一个 root，不用等别的 root，所以应该比 pilot-1 的 7.8 小时墙钟时间短。

直接可用的命令在下面第 4 节。

**先解释几个词：**
- **root（运行目录）**：一个实验条件对应一个独立文件夹。
  - 里面有一份完整的代码副本、它自己的配置文件 `.task-paths.json`、它自己的数据 `prepared/`。
  - 为什么要复制：跑的过程中 plumbing（源码目录）即使被改，也不会影响正在跑的实验。
- **data.py prepare（数据准备）**：从本机已下载的官方 OfficeQA 缓存里，生成这个 root 自己的题目、文档索引和评分文件。
  - 只读本地文件，不联网，用时 8 秒。
  - 标准答案放在 `prepared/private/`，权限 0400（只有文件主人可读），模型看不到。
- **preflight（起飞前检查）**：正式运行前的检查清单，就像飞机起飞前逐项核对。
  - `--offline-only`（只做离线部分）：检查数据哈希、配置、温度、题目顺序、规则，并跑一遍测试。花费 $0。
  - 不加这个参数：先把离线部分再做一遍，然后用一道**合成的假题**真实调用一次 Fireworks。这叫 **readiness smoke（就绪冒烟测试）**，确认付费链路是通的。
  - 通过后写出 `preflight/readiness.json`。`run.py run` 没有这个文件就拒绝启动。
- **barrier（屏障）**：让几个 root 每跑完 5 题就互相等待。b2 只有一个 root，所以屏障 `v9-p2-barrier` 只管它自己，不会等别人。
- **gate 规则**：模型提交答案时，harness 用规则检查答案。
  - W0 目录里有 10 个规则文件，`policy.json` 关掉了 5 条。
  - 我在 root 里真的构造了一个 gate，确认它**只会运行 5 条**：`answer_count`、`later_printing`、`period_match`、`recompute_final`、`scale`。
  - 检查报告里有一个字段 `rules_enabled: 10`，它数的是规则文件自身的开关，没有算 `policy.json`。这不是缺陷，但读的时候别被它误导。
- **nohup … &（后台运行）**：`&` 让命令在后台运行；`nohup` 让它在你关掉终端窗口后也继续跑。
- **export FIREWORKS_API_KEY**：把密钥放进当前 shell 的环境变量，后台进程会继承它。
  - 为什么需要：后台进程没法弹出隐藏输入框问你要密钥。
  - `read -rs` 让你输入时屏幕上不显示，密钥也不会写进任何文件。

**b2 的判定规则（已预注册，跑之前写死）：** 严格答对数和 pilot-1 的 text-static a（31/50）比较。
- ≥ 31：继续，开始规划正式研究；
- ≤ 27：停掉 OfficeQA 的机制路线；
- 28–30：看不出结论，需要 text 和 mech 两臂同时并排再跑一次。

---

`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.
Paid calls: 0. Network: 127.0.0.1 only (the live-Codex test tier's fake upstream). `FIREWORKS_API_KEY` was
absent from the session environment and additionally unset (`env -u`) for every command; no key was read or
printed. No file under `prepared/private/` was opened (only its modes were listed). No source file was edited.

## 1. Root path and configuration

| item | value |
|---|---|
| root | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p2-a` |
| barrier | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p2-barrier` (roots: `v9-p2-a` only; timeout 43,200 s) |
| arm | `mech-static` (l1 tools, controller, seed `rules`, no learner) |
| temperature / top_p | 0 / no `top_p` key (none sent) |
| subset | `pilot-first50`, 50 ids, sha256 `c95f9613…a07d` |
| order seed / repeat | `v9-officeqa-pro133-order` / 1; order sha256 `59b4e268…24b3` (= pilot 1) |
| framework / data cache | `frameworks-v8/next-a48c0f9` / `baseline-work/data-cache/officeqa-official` (as pilot 1) |
| PREREG.md sha256 | `4d550956c88e4c6736c2df746d9d713673eb19e6610813ae9a28b6160a407d9e` (P1 + P2) |
| mech W0 revision | `d4e9c3a1a574fca83ab8c25a71babe5fd0393f68346f2292d845bdc5a9102398` (= addendum P2 pin) |

Checks on the created root:
- `.task-paths.json` differs from `v9-p1-b`'s only in `barrier` (dir `v9-p2-barrier`, roots `[v9-p2-a]`).
- Code is byte-identical to the plumbing: `diff -rq`, excluding `preflight/`, `prepared/`, `.task-paths.json`
  and caches, reports no difference.
- sha256 of `adapters/mech-w0/SEED.md` `8a331dea…`, `policy.json` `0349180f…` and `pilot/metrics.py`
  `156c4d53…` equal the addendum P2 pins.
- `--alt` occurs 0 times in the mech `SEED.md` and in the rendered first-question `AGENTS.md` / `README`
  (`preflight/agents-md-first-question.md`, `preflight/readme-first-question.md`; counted with `grep -c`, not
  printed).
- The gate built from this root's W0 (`Gate(pack, load_rules, load_policy, …)`) evaluates exactly
  `answer_count`, `later_printing`, `period_match`, `recompute_final`, `scale`. The 5 disabled rules are
  `competing_candidates`, `flagged_cells`, `grounded`, `row_coverage`, `table_alternatives`.
  The offline report's `info.w0.rules_enabled: 10` counts the per-rule `enabled` flags only (`arms.check_w0`),
  not the policy; not a defect, but misleading when read alone.

## 2. What was run (all $0)

| step | command (cwd) | result |
|---|---|---|
| 0 | snapshot (path, size, mtime) of `v9-p1-a/b/c`, `v9-p1-barrier`, `v9-p1-metrics.json` and the plumbing | 75,380 + 974 entries |
| 1 | `env -u FIREWORKS_API_KEY $PY -B run.py pilot2-root` (plumbing) | exit 0; created `v9-p2-a`; `paid_calls: 0`; all P2 pins and the addendum accepted |
| 2 | `env -u FIREWORKS_API_KEY $PY -B data.py prepare` (root) | exit 0, 7.9 s: `prepared: true`, 133 tasks, 697 documents, `fixture_only: false`, protocol `officeqa-pro133-v9-mech-static-r1`; `prepared/private/{answers.jsonl, provenance.jsonl, reward.py}` mode 0400 |
| 3 | `env -u FIREWORKS_API_KEY $PY -B preflight.py --offline-only` (root) | exit 0, 3 min 47 s (00:26:55–00:30:43 EDT): **`offline_checks_passed: true`**, 27/27 checks true |
| 4 | snapshot again, `cmp` with step 0 | pilot-1 roots + metrics JSON: identical. Plumbing: identical |

Offline checks (all true): `arm_valid`, `profile_v9`, `data`, `tables_index_matches_corpus`,
`question_count_133`, `base_order_equals_v8`, `subset_hash_matches_manifest`, `barrier_order_registered`,
`barrier_not_aborted`, `frozen_runtime_deep`, `seed_parity_within_budget`, `w0_valid`, `kit_built_for_arm`,
`codex_config_valid`, `settings_v9`, `temperature_sent_equals_declared`, `top_p_sent_equals_declared`,
`root_name_reveals_no_arm`, `frameworks_are_private_copies`, `prereg_present`, and the seven suites.

| suite (in the root, no `V9_SLOW`, as pilot 1) | result |
|---|---|
| kit | 28 passed, 2 skipped |
| pack | 74 passed, 22 skipped |
| core | 79 passed, 4 skipped |
| broker | 63 passed, 2 skipped |
| orchestration | 67 passed, 2 skipped |
| learners | 44 passed, 1 skipped |
| live_codex | 32 passed |
| **total** | **387 passed, 33 skipped (slow-marked), 0 failed** |

The full slow + live tier (435 passed) was run on byte-identical plumbing code by the implementer and the
review (`pilot2.md` §5, `pilot2-review.md`); no plumbing file changed after the review.

Other recorded values (`preflight/offline-checks.json`): question_count 50; model-visible bytes text 2,478,
mech 4,845 (SEED 3,245 + rule templates 1,130 + controller 470; gate frame 257 recorded separately), budget
16,384; Codex `0.155.0-alpha.9.2`; frozen actor runtime deep-verified (11,517 files).

Barrier `v9-p2-barrier/order.json`: count 50, repeat 1, order `59b4e268…24b3`, first root `v9-p2-a`, no ABORT.
No `preflight/readiness.json` exists yet (expected: only the paid step writes it).

## 3. What the paid readiness step needs

`preflight.py` (without `--offline-only`) in the root:
1. **The key.** `FIREWORKS_API_KEY` in the environment, or typed at its hidden prompt. It is not written to disk.
2. **The offline checks again** (~4 min; the same list as above). If any fails, it exits 1 before any paid call.
3. **One paid synthetic smoke** (`run.smoke`). It runs one synthetic marker question through this root's arm
   (`mech-static`) at T=0 with no top_p. Pilot-1 b's smoke cost $0.0021.
4. **Output.** It writes `preflight/readiness.json` and prints `{"readiness_passed": true}`, exit 0.
   The file records the top-level source hashes, the `harness_core/`, `packs/` and `adapters/` revisions,
   and the PREREG sha256.

`run.py run` calls `require_readiness()` and refuses to start if one of these holds:
- the file is missing or not passing;
- any hashed source changed after the smoke (top-level `*.py`, `PROTOCOL.md`, `PREREG.md`, `.task-paths.json`, or the three code trees).

So **no file in `v9-p2-a` may be edited between the smoke and the launch**. The plumbing's
`preflight/smoke-01` does not count: `run.py` reads only this root's own readiness file.

## 4. Exact launch command (paid; ≈ $5; needs your explicit go and your key)

The smoke and the run are chained with `&&`, so the run starts only when the smoke passes:

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
cd /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p2-a
read -rs FIREWORKS_API_KEY && export FIREWORKS_API_KEY      # typed hidden; never written to disk
$PY -B preflight.py && (nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)
```

- `preflight.py` should print `{"readiness_passed": true}`. If it prints `false` or exits non-zero, nothing
  paid beyond the smoke happens. Read `preflight/readiness.json` (checks and smoke status; no answers).
- `run.py run` writes `process.json` (pid) and starts `caffeinate` for itself. It runs 50 episodes in
  10 blocks of 5. At the end, `finalize` seals the predictions (`private-evaluation/predictions-seal.json`) and grades them.

**While it runs** (all $0; from the root):

```
$PY -B run.py cost | head -c 600; echo      # known_cost_lower_bound_usd so far
open report.html                            # progress; shows no questions or answers
kill -INT $(python3 -c "import json;print(json.load(open('process.json'))['pid'])")   # stop (writes ABORT)
$PY -B run.py resume --clear-abort          # continue at the block level after a stop
```

**Expected cost and time.**
- Cost: pilot-1 b's `known_cost_lower_bound_usd` was $5.006 for the actors and $0.002 for the smoke (standard prices; not an invoice).
- Time: pilot-1 b's episodes took 4.1 h in sum (median 135 s, max 2,797 s, none at the 3,600 s cap).
  Its wall clock was 7.8 h because of barrier waits for a and c. b2 has no other root, so expect ≈ 4–5 h
  plus ~5 min for the smoke. The theoretical worst case is 50 × 3,600 s.

**After completion** (from the plumbing, $0):

```
cd /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p2-a --json ../v9-p2-metrics.json
(cd ../v9-p2-a && $PY -B run.py audit)
```

Pass only `v9-p1-a` and `v9-p2-a` to `metrics.py`. Adding `v9-p1-b` would pool b and b2 episodes in the §7
table (`pilot2-review.md` note 3). The output ends with `P2 VERDICT: PROCEED | INCONCLUSIVE | STOP`
(≥ 31 / 28–30 / ≤ 27 strict correct).

## 5. Files written by this step

- `v9-p2-a/` (the root: the code copy, `.task-paths.json`, `prepared/`, `preflight/{offline-checks.json,
  data-verification.json, agents-md-first-question.md, readme-first-question.md}`);
- `v9-p2-barrier/order.json`;
- this report.

The preflight log and the metadata snapshots are in the session scratchpad, outside the project.
