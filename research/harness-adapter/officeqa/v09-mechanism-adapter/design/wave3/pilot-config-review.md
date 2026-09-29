# v9 wave 3: adversarial review of the pilot configuration (2026-09-28)

## 中文摘要（给用户）

**结论：** 实现正确，可以进入下一步（`run.py pilot-roots`）。我逐项独立核对了 5 个要求，没有发现会影响 pilot 结果的缺陷。我只修了 **1 个小漏洞**（审计覆盖面）。修完后完整测试仍然 **419 passed、0 failed**（全开档，331 s）。PREREG 和所有钉住的哈希**都没有变**。

**先解释几个词：**
- **adversarial review（对抗式审查）**：假设实现有错，主动去找它会在哪里出错，而不是只看测试是否通过。
- **top_p（核采样）**：模型只在"累计概率达到 p 的候选词"里抽样；0.95 = 砍掉最不可能的 5% 尾巴。
- **request body（请求体）**：broker 发给 Fireworks 的那段 JSON，采样参数就写在里面。
- **byte-identical（逐字节相同）**：两份输出的每一个字节都一样，用来证明"没声明 top_p 时和 v8 完全一样"。
- **hash chain（哈希链）**：PREREG 的 sha256 指纹被 readiness 和每个 root 的 freeze 记录；附录里又钉住了题单、选题脚本和 metrics 脚本的指纹。任何一环改动都会被发现。

**我核对了什么、结果如何：**
1. **top_p 真的进了每一个请求体。**
   - broker 只有一个构造上游请求体的地方（`normalized`）。controller 的重问（re-query）是复制这个请求体再改 input，所以也带 top_p。
   - 没声明时：请求体里完全没有 top_p 键。我查了 smoke-01 里真实 Codex 发出的请求：Codex 自己也不发 top_p，所以和 v8 一样。
   - 与 v8 逐字节比较的测试（`test_v9_broker::test_1`）仍然通过。
2. **前 50 题是"不看答案"选出来的，且和历史 50 题完全一致。**
   - 我自己写了一段独立脚本，只读 task id 和 ordinal（题号）：
     - v8 manifest 的顺序 = 按种子重新计算的顺序；
     - 前 50 个 = `first50.json` 的 50 个；
     - v6f prompt、v4 gold、v5 mwm、v7 cards 四个历史 root 都恰好跑了这 50 题，而且顺序就是 1–50。
   - 题单指纹 `c95f9613…a07d` 我重新算过，一致。loop-prone 重叠 = **17/50**（我重跑确认）。
3. **温度规则写得清楚，metrics.py 算的正是它。**
   - "c 的 loop run ≤ 0.5 × a 的"，并且"c 的严格正确题数 − a 的 ≥ −2"，两个都满足才用 T=1.0 / top_p 0.95。
   - a 没有 loop run 时判为不满足（→ T=0），这一点附录里写明了。
   - go/no-go 阈值和退出码都没改。
4. **哈希链一致。**
   - PREREG 前 137 行和改动前逐字节相同，后面接的正好是 `ADDENDUM.md` 全文。
   - 新 sha256 = `baa53c64…2fa3`。附录里钉住的 4 个哈希和磁盘上的文件全部一致。
   - 目前还没有 readiness 或 freeze 记录旧哈希，所以没有过期的记录。
5. **完整测试**：改前 419 passed，改后仍是 419 passed。

**我修了什么（1 处）：**
- **问题：** `full_audit.py` 遇到"actor 被杀时还在路上的请求"（有 request.json、没有 transport.json）会直接跳过，不检查它的 temperature 和 top_p。可这个请求体已经发出去了。
- **修复：** 现在先检查采样参数，再跳过。文件坏了就记为审计失败，而不是让审计崩溃。
- **测试：** 我扩展了已有的测试，确认这种请求的 top_p / temperature 被改动时审计会报错。
- 这个文件不在 PREREG 钉住的范围内，所以 PREREG 哈希不变。

**需要你知道的（都不是缺陷，是设计层面的提醒）：**
- **噪声：** T=0 也不是完全确定的。你之前的基线审计显示，同一设置跑 3 次，准确率可以差好几题。所以"差 2 题以内"和"loop run 少一半"在 50 题上都会受随机性影响。规则已经预注册，这里只是提醒你解读时注意。
- **只在 text-static 上测：** 规则是在 text-static 上测出来的。mech 臂在 T=1.0 下的表现，以及 gate 在 T=1.0 下的精确度，pilot 都没有测。实现者也提到了这一点。
- **第一次真实发送 top_p：** `v9-p1-c` 的 readiness smoke 将是第一次把 top_p 0.95 发给 Fireworks。smoke-01 跑的是改动前的代码。

---

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.
Reviewed against the pre-change originals in the session scratchpad (`orig-pilot-config/`); the files changed are exactly the backed-up set plus the new `pilot/select_first50.py` and `pilot/first50.json`.

## 1. top_p reaches every upstream body when set; nothing changes when unset

| check | how | result |
|---|---|---|
| single construction point | `grep normalized(` over the plumbing: the handler (`broker.py:993`) plus the two probes `campaign.broker_temperature` / `broker_top_p` | top_p is passed at the only production call site |
| controller re-queries | `broker._after`: `revised = dict(outgoing)` then only `input` / `max_output_tokens` change | re-query bodies carry top_p; `test_real_broker_sends_top_p_only_when_declared` asserts both upstream bodies `(1.0, 0.95)` |
| every actor episode | call sites of `episode` / `execute_episode`: `Campaign.run_one` (scored + verify) and `run.smoke`; `execute_episode` passes `top_p` to the broker factory and `run_actor` from the same `options` dict | complete; `cards_learning.py`'s broker and `client.py` are learner-side and untouched (as specified) |
| unset = v8 | `normalized` adds the key only when `top_p is not None`; `changes.top_p` only when sent; real Codex bodies in `preflight/smoke-01/*/api/*/request.json` have no `top_p` key (keys checked only) | byte-identical; `test_v9_broker::test_1_controller_none_is_byte_identical_to_v8` passes |
| records | `freeze.json` settings `top_p` / `broker_top_p`, spec `top_p` only when declared, `invocation.json` `top_p`, `state.json`, readiness, smoke `sampling_ok` | present |
| refusals | `Campaign.load` (broker would send another value), `verify_freeze` (resume under another/no top_p), `parse_top_p` / `Broker` (bool, 0, >1, string, NaN) | tested |
| readiness binding | `.task-paths.json` is in `preflight.source_names()`, so editing top_p after readiness makes `run.py run` refuse | ok |

**Defect fixed (low severity, audit coverage).** `full_audit.Audit.records` skipped a call with `request.json` but no `transport.json` (in flight at the actor kill) before the temperature and top_p checks, although that body was sent. Now the sampling checks run on every `request.json` first, and an unreadable one is a failed check (`<episode> api/NNN request.json readable`) instead of an exception. `tests/test_v9_pilot.py::test_top_p_campaign_freezes_records_audits_and_refuses_a_mismatch` now also deletes one `transport.json`, sets `top_p` 0.9 / temperature 0 in that body, and asserts both named checks fail. `full_audit.py` sha256 is now `3f1ac23cc8c1ceaa9c41facf9ed176a1b6cf01e80d3c8b4531a7e8e352d06c84`. It is not pinned by the addendum, and no readiness exists yet.

## 2. The first 50: label-free and identical to the historic 50

Independent re-check (a separate script; reads only the v8 manifest's `task_ids` / `selection.seed` and `task.json` `task_id` / `ordinal`; the v8 `task.json` keys are `ordinal, question, task_id`, so no answer is in them):

| check | result |
|---|---|
| v8 manifest order = ascending sha256(seed NUL uid), uid tie-break, seed `metaworld-officeqa-pro-codex70-online-v1` | true (133 ids) |
| manifest[:50] as a set = `first50.json` `task_ids` | true; ids ascending, 50 distinct, ordinals = base-order positions |
| `subset_sha256` recomputed | `c95f9613db9e37effcdaa4de81875f3c045fabb40edfc9c299ca5e60c1b5a07d` (matches) |
| historic roots, ordinal 1–50 = manifest[:50] in order | v6f prompt-only, v4 gold, v5 mwm, v7 cards: all true (v6d/v6e have no `runs/baseline`) |
| `select_first50.py --check --overlap` | `matches_file: true`, 5 historic matches; overlap 17 of 50 in the eligible 46 (by arms looped: 0→33, 1→4, 2→9, 3→4); 12 of the old 20 in the 50 |

## 3. The rule is unambiguous and metrics.py computes exactly it

- Addendum text matches the user's rule verbatim; the operational definitions close the open points: loop run = ≥ 3 completed `command_execution` items with the same `normalize_command` (whitespace collapse) string; `loops_c ≤ 0.5 × loops_a`; `loops_a = 0` → not met; `right_c − right_a ≥ −2`; T=0 = no top_p; evaluated only on completed, sealed, graded roots.
- `pilot/metrics.temperature_rule` implements exactly those: cells identified by arm `text-static` and frozen `(temperature, top_p)` = `(0, None)` / `(1.0, 0.95)`; exactly one of each or "not computable"; same subset hash across roots (`compute`) and equal episode counts; `right` is the controller-side strict grade of the sealed answer, which must agree with the official grades file or the script refuses.
- Go/no-go constants (0.60, 2×, 3×, 0.20, 0.05, 0.50) and the exit codes are unchanged from the original `metrics.py`. The verdict does not change the exit code.
- Parametrised tests cover both boundaries (exactly 50%, exactly −2), 40%, −3, and `loops_a = 0`.

## 4. PREREG hash chain

- `head -n 137 PREREG.md` is byte-identical to the original (sha256 `7a7824a4…b939`); `tail -n +138 PREREG.md` equals `pilot/ADDENDUM.md` byte for byte.
- PREREG.md sha256 `baa53c64c1c57ff499afc0955aa16a5aa21af6c8e4a5c5f9219bf975cae32fa3`.
- Pins in the addendum match the files on disk: list `c95f9613…`, `first50.json` `00ff0832…`, `select_first50.py` `318ab73c…`, `metrics.py` `dc302abc…`. `first50.json` `script_sha256` = `318ab73c…`.
- No readiness, freeze or other document records the old PREREG hash (`preflight/smoke-01/verification.json` records source hashes and temperature only, no PREREG hash). My fix touched neither PREREG nor a pinned file.

## 5. Full suite

| run | result |
|---|---|
| `V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B -m pytest -q -p no:cacheprovider tests` before my fix | 419 passed (327 s) |
| same, after my fix | **419 passed, 0 failed** (331 s) |

## 6. Observations (no change made)

1. **Noise vs the rule's margins.** T=0 is not deterministic on this provider (the baseline variance audit found several-question spreads between identical runs). On 50 questions, "≤ 2 fewer correct" and "≥ 50% fewer loop runs from ~10" are both within plausible run-to-run noise. The rule is pre-registered as the user decided, so this only affects how its verdict is read.
2. **Scope of the rule.** Measured on text-static only; mech arms and the gate's precision at T=1.0 are not piloted (also flagged by the implementer).
3. **First paid top_p call.** `smoke-01` predates the change; the `v9-p1-c` readiness smoke is the first body with `top_p: 0.95` to Fireworks, and it fails closed (`sampling_ok`, `passed`) before any pilot money if the provider rejects or drops it. The broker records what was sent, not what the provider applied.
4. `native_request_settings` in `transport.json` does not list an incoming `top_p`. That is harmless, because Codex sends none and the audit requires an undeclared top_p to be absent from every body.

No paid call; only 127.0.0.1; no key or credential file read; no reference answer or grader opened on real data; no v8 root, frameworks-v8, MetaHarnessAdapter* or opus-officeqa-133 file modified; no pilot root created.
