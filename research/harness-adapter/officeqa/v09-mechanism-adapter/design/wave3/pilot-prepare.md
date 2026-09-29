# v9 wave 3: pilot roots prepared ($0) — 2026-09-28

## 中文摘要（给用户）

**结论：** 三个 pilot 目录已经建好，数据也已准备好。其中 **a、b 两个已通过全部免费检查**。**c 有 1 个测试失败**，它会挡住 pilot 的启动。原因是测试本身写死了一个假设，不是代码或采样参数出错。修复只需要改测试文件里的 1 行，我已经在不改文件的情况下验证过这个修复有效。按你的要求，我**没有做任何付费调用**，也没有改任何代码。

**先解释几个词：**
- **root（运行目录）**：一个实验条件对应一个独立的文件夹。里面有一份完整的代码副本、它自己的配置 `.task-paths.json` 和它自己的数据。
- **preflight（起飞前检查）**：正式运行前的检查清单。
  - `--offline-only` 只做免费部分：数据哈希、配置、测试套件等。
  - 不加这个参数时，检查的最后还会跑一次付费的 **readiness smoke（就绪冒烟测试）**：用一道合成的假题，真实调用一次 Fireworks，花费几美分。
  - 通过后会写出 `preflight/readiness.json`。`run.py run` 没有这个文件就拒绝启动。
- **barrier（屏障）**：三个 root 每跑完一个 block（5 题）就在这里互相等待，全部到齐后才一起进入下一个 block。所以任何一个 root 起不来，另外两个也会卡住。
- **live_codex 测试档**：用真实的 Codex 程序连接本机 127.0.0.1 上的假上游服务器，花费为 $0。

**每个 root 的状态：**

| root | 条件 | 数据准备 | 免费检查 | 付费就绪测试 |
|---|---|---|---|---|
| `v9-p1-a` | text-static，T=0，不发 top_p | ✅ | ✅ 全部通过 | 未跑（按要求） |
| `v9-p1-b` | mech-static，T=0，不发 top_p | ✅ | ✅ 全部通过 | 未跑（按要求） |
| `v9-p1-c` | text-static，T=1.0，top_p 0.95 | ✅ | ❌ `suite_live_codex` 有 1 个测试失败 | 未跑；修好之前也跑不了 |

**c 为什么失败：**
- 测试 `test_live_codex_smoke_path_at_zero_cost` 断言：第一次 smoke 用的是默认设置，即不发 top_p。
- 这个假设只在 plumbing（没有配置文件的源码目录）里成立。
- 在 c 目录里，smoke 会读取 c 自己声明的 T=1.0、top_p=0.95，并且正确地发送了它们（`sampling_ok` = True）。
- 所以真正出错的是测试的假设，而不是代码。
- **修复方法：** 在该测试里第一次调用 `run.smoke` 之前，加一行 `monkeypatch.setattr(CP, 'CAMPAIGN', None)`。
- 我用一个临时插件（放在 scratchpad，不修改 root 里的任何文件）在 c 里做了同样的替换，测试通过。
- `tests/` 不在 PREREG 钉住的哈希里，也不在 freeze 或 readiness 的哈希范围内。所以这个修复不会改变任何哈希。

**要启动 pilot，还需要你做 3 件事：**
1. 同意并应用上面这 1 行测试修复。改在 plumbing 和三个 root 里，保证四份代码保持一致。
2. 批准每个 root 各跑一次付费就绪测试（`preflight.py`，每个 root 几美分，约 5 分钟）。
   - plumbing 里的 smoke-01 **不能代替**它：`run.py` 只认每个 root 自己的 `preflight/readiness.json`。
   - 而且 smoke-01 是用加入 top_p 之前的代码跑的（`acting.py`、`broker.py`、`campaign.py`、`run.py` 的哈希都已经变了），它从来没有发送过 top_p。
3. 批准 pilot 本身（无预算上限，预计约 $30–45），并在启动的那个 shell 里输入 Fireworks 密钥。

**预计时长：** 三个 root 并行跑，大约 **6–10 小时**（按 v8 在这 50 题上的实际耗时估算，见 §4）。

---

`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`,
`P=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing`.
Paid calls: 0. Network: 127.0.0.1 only (the live-Codex tier's fake upstream). `FIREWORKS_API_KEY` was unset
(`env -u`) for every command; no key was read or printed. No reference answer, grader or `prepared/private/*`
file was opened (only their modes were listed). No source file was edited.

## 1. Root paths

| root | path | arm | temperature | top_p |
|---|---|---|---|---|
| a | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p1-a` | text-static | 0 | (key absent, none sent) |
| b | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p1-b` | mech-static | 0 | (key absent, none sent) |
| c | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p1-c` | text-static | 1.0 | 0.95 |
| barrier | `/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/v9-p1-barrier` | roots a, b, c; timeout 43,200 s | | |

All three have: profile v9, repeat 1, order seed `v9-officeqa-pro133-order`, subset `pilot-first50`
(50 ids, sha256 `c95f9613…a07d`), framework `frameworks-v8/next-a48c0f9`, data cache
`baseline-work/data-cache/officeqa-official`. Code is byte-identical to the plumbing (`diff -rq` excluding
`preflight/`, `prepared/`, `.task-paths.json` and caches: no differences). PREREG.md sha256 in every root:
`baa53c64c1c57ff499afc0955aa16a5aa21af6c8e4a5c5f9219bf975cae32fa3`.

## 2. What was run (all $0)

| step | command | result |
|---|---|---|
| 0 list check | `$PY -B pilot/select_first50.py --check --overlap` | `matches_file: true`; historic 50 matches: v4-gold, v5-mwm, v6f-mwm, v6f-prompt, v7-cards; overlap 17/50 in the eligible 46 |
| 1 addendum | `grep -c "pilot-list-sha256: c95f…a07d" PREREG.md` | 1. The pinned hashes on disk still match the review: PREREG `baa53c64…`, first50.json `00ff0832…`, select_first50.py `318ab73c…`, metrics.py `dc302abc…`, full_audit.py `3f1ac23c…` |
| 2 full tiers (plumbing) | `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` | **419 passed** in 338.6 s (13:02–13:08 EDT) |
| 3 roots | `$PY -B run.py pilot-roots` | created a, b, c; `paid_calls: 0` |
| 4a data | `(cd ../$r && $PY -B data.py prepare)` per root | each: `prepared: true`, 133 tasks, 697 documents, `fixture_only: false`, ~8 s. `prepared/private/{answers.jsonl, provenance.jsonl, reward.py}` are mode 0400. The manifest records subset `pilot-first50` with the same sha256 |
| 4b offline preflight | `(cd ../$r && $PY -B preflight.py --offline-only)` per root, sequentially | a: **pass** (242 s). b: **pass** (234 s). c: **FAIL**, exit 1, only `suite_live_codex` false (234 s) |

Per-root offline results (from `preflight/offline-checks.json`):

| root | passed | T | top_p | order sha256 | questions | failing checks |
|---|---|---|---|---|---|---|
| a | true | 0 | None | `59b4e268…24b3` | 50 | none |
| b | true | 0 | None | `59b4e268…24b3` | 50 | none |
| c | false | 1.0 | 0.95 | `59b4e268…24b3` | 50 | `suite_live_codex` (1 failed, 31 passed) |

The barrier's `order.json` has repeat 1, count 50, order `59b4e268…`, registered first by a. All three roots
agree. It has no ABORT. `temperature_sent_equals_declared`, `top_p_sent_equals_declared`,
`subset_hash_matches_manifest` and `base_order_equals_v8` are true in all three, including c. No
`readiness.json` exists in any root, which is expected because no paid smoke was run.

Tier coverage inside the roots follows README step 4 exactly (no `V9_SLOW` set). Slow-marked tests are
therefore skipped there: kit 2, pack 22, core 4, broker 2, orchestration 2, learners 1. The full slow + live
tier passed 419/419 in the plumbing on byte-identical code.

## 3. Blockers

### 3.1 v9-p1-c: one test assumes it runs outside a campaign root (must fix before launch)

`tests/test_v9_orchestration.py::test_live_codex_smoke_path_at_zero_cost`, line 1355:

```
assert all(r['sampling_ok'] for r in result['results']) and result['top_p'] is None     # T=0, no top_p sent
E       assert (True and 0.95 is None)
```

**Cause.**
- The first `run.smoke(...)` in the test takes its temperature and top_p from `campaign.CAMPAIGN`.
- In the plumbing, `campaign.CAMPAIGN` is None, so the smoke uses the defaults (T=0, no top_p).
- In `v9-p1-c`, `campaign.CAMPAIGN` is that root's spec, T=1.0 and top_p 0.95.
- The smoke therefore correctly sent (1.0, 0.95), and `sampling_ok` is True for every body.
- Only the hard-coded "no top_p" expectation fails.
- Roots a and b pass because their spec equals the defaults.

This is a defect in the test's environment assumption. The code and the sampling path are correct.

**Why it blocks.**
- `preflight.py` without `--offline-only` exits before the paid smoke when the offline checks fail.
- So `v9-p1-c` cannot get `preflight/readiness.json`, and `run.py run` in c refuses to start.
- Roots a and b would then wait at the barrier after block 1 for up to 12 h and abort.
- **Do not start the pilot until c passes.**

**Proposed fix (one line, test only).** Make the same change in the plumbing and in all three roots, so the
four copies stay identical. In `test_live_codex_smoke_path_at_zero_cost`, just before
`result = run.smoke(KEY, 'smoke-t', ['plain', 'mech-static'], ...)`, add:

```python
            monkeypatch.setattr(CP, 'CAMPAIGN', None)     # the first smoke checks the defaults, in any root
```

**Verified without editing anything.**
- I ran the test in `v9-p1-c` with a scratch pytest plugin (`scratchpad/plug/plumbctx.py`). It applies the same
  `monkeypatch.setattr(campaign, 'CAMPAIGN', None)` as an autouse fixture:
  `-p plumbctx -m live_codex -k test_live_codex_smoke_path_at_zero_cost` → **1 passed**.
- The test's second half, the cell-c smoke asserting `(1.0, 0.95)` on every body, is unchanged and still passes.

**Hash impact: none.**
- `tests/` is not in `preflight.source_names()`, which covers only top-level `*.py`, PROTOCOL.md, PREREG.md and
  `.task-paths.json`.
- It is not in `campaign.CODE_TREES` (`harness_core`, `packs`, `adapters`).
- It is not pinned by the addendum.
- So the PREREG hash, the freeze and readiness are unaffected.

After the fix, re-run `(cd ../v9-p1-c && $PY -B preflight.py --offline-only)` (~4 min). Step 5 below also
re-runs the offline checks.

### 3.2 Per-root paid readiness smoke (not run, per instructions)

`run.py run` calls `require_readiness()`. That check needs `<root>/preflight/readiness.json` with:
- `passed: true`;
- unchanged top-level source hashes;
- unchanged `harness_core/`, `packs/` and `adapters/` revisions;
- `prereg_sha256` equal to the root's PREREG.md.

Only `preflight.py` without `--offline-only` writes that file, and it does so only after a passing paid smoke.

`preflight/smoke-01` in the plumbing cannot stand in for it:
- `run.py` reads only the root's own `preflight/readiness.json`, and smoke-01 wrote no readiness file.
- smoke-01 (12:09, T=0, five arms, all passed) predates the top_p change. Its recorded source hashes differ now
  for `acting.py`, `broker.py`, `campaign.py` and `run.py` (`controller_runtime.py`, `kit.py` and
  `rate_control.py` are unchanged).
- smoke-01 never sent top_p. The c smoke is the first body with `top_p: 0.95` to Fireworks. If the provider
  rejects it, that smoke fails closed before any pilot money is spent.

What each root needs:
- the key, from `FIREWORKS_API_KEY` or the hidden prompt;
- about 4 min of offline checks;
- one synthetic marker question through its own arm at its own T/top_p, which costs cents.

Command (README step 5, after 3.1 is fixed):

```
cd $P
for r in v9-p1-a v9-p1-b v9-p1-c; do (cd ../$r && $PY -B preflight.py) || break; done
```

Expect `{"readiness_passed": true}` from each root.

`readiness.json` records `prereg_pending_decisions`: six `[DECISION PENDING …]` lines for the confirmatory
study, such as repeats, confirmation sets and CPI-U. They are recorded but not checked, so they do not block
the pilot.

### 3.3 The key for a background launch

A background (`nohup … &`) process cannot answer the hidden prompt, so `FIREWORKS_API_KEY` must be exported
in the launching shell (see 4).

## 4. Launch command (all three roots in parallel; paid, no cap)

Run this only after 3.1 is fixed and all three readiness smokes pass:

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
P=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
cd $P
read -rs FIREWORKS_API_KEY && export FIREWORKS_API_KEY     # typed hidden by the user; never written to disk
for r in v9-p1-a v9-p1-b v9-p1-c; do
  (cd ../$r && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)
done
```

Each root writes `process.json` (pid) and starts `caffeinate` for itself. For cost after each block, see README
step 6 (`run.py cost`). To stop, use the README step 6 `kill -INT` per root. Then run
`run.py resume --clear-abort`.

**Expected duration.**

Each root runs its 50 episodes sequentially, in 10 blocks of 5. The barrier makes every block wait for the
slowest root. Static arms have no learner, so block time is actor time only.

Estimate from the v8 per-episode `elapsed_seconds` on these same 50 questions (timing fields only):

| v8 arm | sequential actor time | median per question | questions at the 3,600 s cap |
|---|---|---|---|
| none | 5.7 h | 87 s | 3 |
| cards | 7.3 h | 96 s | 2 |
| prompt | 7.8 h | 115 s | 4 |

- **Barrier-paired estimate.** Take the slowest of the three v8 arms in each pilot block (the pilot's actual
  repeat-1 blocks), then sum over blocks: **≈ 9.5 h**.
- **Expected range.** About **6–10 h**, plus about 15–20 min for the three readiness preflights.
- **Uncertainty.** It is driven by the questions that hit the 3,600 s cap, meaning loops. Cell c at T=1.0 is
  unmeasured. The mech-static gate re-queries also add time.
- **Theoretical worst case.** 50 × 3,600 s ≈ 50 h per root.
- **Barrier timeout.** Any single barrier wait longer than 12 h aborts the group.

**Cost.** About $30–45 at v8 prices (README, no cap), plus cents for the three smokes.
