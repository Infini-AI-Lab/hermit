# v9 wave 3: adversarial review of the pilot-2 change (re-pilot b2), 2026-09-29

## 中文摘要（给用户）

**结论：没有发现需要修的缺陷，我没有改任何代码或文件。** 这次改动可以直接用来跑 b2（重跑 b）。

我逐项重新核对了一遍，不是只看实现者的报告：

1. **mech 的 W0 = text 的 W0 + 收尾说明，已核对。**
   - W0 = 第 0 版适配器，也就是每个实验臂开跑时模型拿到的初始知识。
   - `adapters/mech-w0/SEED.md` 的开头逐字等于 `adapters/text-w0/SEED.md`（2,478 B），接一个空行，再接收尾说明。
   - 模型能看到的地方都不再出现 `--alt`：SEED、README 的用法行、gate 的用法行、`submit --help`。
   - 模型可见字节数由 `arms.check_w0` 重新算出，和补充条款一致：
     - text 2,478 B；
     - mech 4,845 B = 3,245 SEED + 1,130 规则模板 + 470 控制器模板；
     - 预算 16,384 B，没有超；gate frame 257 B 单独记录。
2. **关规则的回放可以复现，顺序符合预注册。**
   - 我在 `v9-p1-b` 上重跑了 `pilot/select_rules_p2.py`，输出写到临时目录，没有写进任何 root。
   - 生成的 JSON 和提交的 `pilot/pilot2-rule-selection.json` **完全相同**。
   - 86 次闸门检查全部离线重算一致（86/86），也就是"线上 = 线下"。
   - 步骤依次为：12/21 → 10/21 → 9/21 → 6/21 → 5/21 → 3/21（14.3%）。
     - 分子是还启用的规则在答对题上触发的题数，分母是答对的题数（21）。
     - 最后一步两条限制同时满足：误报率 ≤ 20%，由规则造成的"对改错" = 0。
   - `competing_candidates` 第一个被强制关掉。我数了 pilot 1 里它所有的 Offer（替代答案），7 次全部来自模型自己的 `--alt`，其中包括第 8、21 题这两次对改错。
   - `policy.json` 通过了 `validate_policy`（在 POLICY_RANGES 范围内，没有未知键）。
   - gate 在运行时确实跳过这 5 条规则：`campaign.build_controller` 调用 `arms.load_policy`，再交给 `Gate`，`Gate` 会过滤掉 `disabled_rules`。
3. **补充条款 P2 已核对：只追加了一次，在 P1 之后，前面的原文一个字没动。**
   - 新 `PREREG.md` 以 pilot-1 的 PREREG 原文开头。
   - 条款文字 = `pilot/ADDENDUM-P2.md` 逐字全文，在文件末尾，只出现一次。
   - 6 个 sha256 pin（哈希钉）我逐个重算，全部相符。
   - pilot-1 的数字（31/20/24、NO-GO 的各项、温度裁决 T=0）和 `v9-p1-metrics.json` 一致。
   - 决策规则写得清楚，没有歧义：b2 ≥ 31 继续；b2 ≤ 27 停；28–30 不确定，需要两臂同时配对重跑。时间漂移的注意事项也写进去了。
4. **新建 root 的命令已核对。**
   - 我让 `run.pilot2_root` 在临时目录里真的建了一次 `v9-p2-a`。
   - 它的 `.task-paths.json` 和 `v9-p1-b` 相比，只有 barrier（只管这一个 root）和两个路径参数不同。
   - arm、题目子集哈希、顺序种子、repeat 1、温度 0、不发 top_p，全部一致。
   - 名字 `v9-p2-a` 看不出是哪个臂。
5. **metrics 已核对。**
   - 用新的 `pilot/metrics.py` 重算 pilot-1 的三个 root，得到的 JSON 去掉新增字段以后，和已保存的 `v9-p1-metrics.json` **完全相同**。所以 P1 那部分代码没有变。
   - P2 裁决只认冻结 W0 版本等于 P2 版本的 mech root，所以 pilot-1 的 b 不会被误当成 b2。
6. **全部测试通过：435 passed**（`V9_SLOW=1 V9_LIVE_CODEX=1`，用时 5 分 33 秒）。
   - 整个复查过程中，四个 pilot-1 目录和 `v9-p1-metrics.json` 都没有被修改。
   - 没有打印或写出任何答案值，没有付费调用，没有读取密钥。

**需要你知道的三点（都不是缺陷）：**
- **规则选择很脆弱**，实现者已经如实写进条款：换两种同样合理的定义，会关掉不同的规则。三种定义都会关的只有 `competing_candidates`、`grounded`、`table_alternatives`。所以 b2 只能回答"这组规则 + 完整建议有没有用"，回答不了"哪条规则好"。
- **不要把 b 和 b2 一起传给 metrics。** 如果运行 `metrics.py ../v9-p1-a ../v9-p1-b ../v9-p2-a`，§7 的 go/no-go 表会把 b 和 b2 的 episode 合并计算。P2 裁决本身不受影响。按条款写的命令 `metrics.py ../v9-p1-a ../v9-p2-a` 是对的。
- **b2 还没有开始跑。** 你说的是"改完跑 b"，但付费那两步需要你的 Fireworks 密钥。我不读密钥，所以没有替你启动。命令在本文末尾，前两步免费。

## English

### Verdict

No confident defect found; no file was changed by this review. The change is ready for the b2 run.

### What was verified, and how

| item | check | result |
|---|---|---|
| mech SEED = text SEED verbatim + finishing text | byte prefix comparison; `arms.w0_files` = committed tree (`check_w0`) | holds; separator is one blank line |
| no `--alt` advertised | grep of SEED, `campaign.SUBMIT_README`, `gate.USAGE`, `submit --help` output, submit.py docstring | none; flag still parsed (`argparse.SUPPRESS`) |
| byte sizes | `arms.check_w0` for both arms | text 2,478; mech 4,845 (3,245 + 1,130 + 470); gate frame 257; budget 16,384; 532 B of disabled-rule templates still counted (conservative) |
| rule selection reproduces | re-ran `pilot/select_rules_p2.py ../v9-p1-b --out <scratch>` with recheck | JSON identical to committed file; recheck 86/86 identical, enabled-only replay 86/86 |
| pre-registered order | rule table and steps | ascending engine precision (0.50, 0.60, 0.67, 0.71 tie broken by forced cc, 0.71), stop at first step where (a) 3/21 = 14.3% <= 20% and (b) 0 attributable r->w hold |
| competing_candidates offers | classified every pilot-1 cc finding by offer source (no values) | 7/7 were "your --alt", in episodes 002, 008, 018 (x2), 021, 025, 034 |
| policy valid | `harness_core.rules.validate_policy` | [] (valid); broker-side `checked_policy` covered by tests |
| disabled rules actually skipped | `campaign.build_controller` -> `arms.load_policy` -> `Gate(... policy)` filters `disabled_rules` | yes; test `test_gate_skips_the_policy_disabled_rules_of_the_w0` |
| addendum | prefix = pilot-1 PREREG.md (sha baa53c64...); ends with ADDENDUM-P2.md; heading count 1 | appended once, after P1, dated 2026-09-28/29 |
| pins | sha256 of PREREG, SEED, policy, selection file/script, metrics; `campaign.revision(adapters/mech-w0)` | all match (d4e9c3a1..., 8a331dea..., 0349180f..., bddd065a..., c26046e6..., 156c4d53...) |
| pilot-1 numbers in addendum | against `v9-p1-metrics.json` | all match (31/20/24; criteria 1-5; T=0 verdict) |
| single-root command | `run.pilot2_root` into a scratch parent with dummy framework/cache dirs | one root `v9-p2-a`; config equals `v9-p1-b`'s except barrier (this root only) and the two paths; no top_p key; refuses without addendum / stale pin (tests) |
| metrics P1 unchanged | new `metrics.py` on a, b, c; JSON minus new fields vs `v9-p1-metrics.json` | identical |
| metrics P2 verdict | code review + boundary tests (>= 31 / 28-30 / <= 27); b2 identified by frozen W0 revision | correct; P1 roots give "not computable" |
| no answer values | selection JSON string scan (only rule ids, procedure text, a hash) | clean |
| full suite | `V9_SLOW=1 V9_LIVE_CODEX=1 ... pytest -q tests` | 435 passed in 333 s |
| pilot-1 roots untouched | `find -newer ADDENDUM-P2.md` over v9-p1-a/b/c/barrier and metrics json, after all my runs and the suite | nothing newer |

### Notes (not defects)

1. **Selection fragility** (already disclosed in the addendum): the `final_grade` and `first_check` definitions pick different sets. Only `competing_candidates`, `grounded` and `table_alternatives` are common to all three. With 21 right episodes, b2 tests "this rule set + full advice", not individual rules.
2. **Criterion (a) is computed on engine fires**, which bound the shown findings from above (one finding per round). This is conservative for the §5 limit.
3. **Merging hazard.** `pilot/metrics.py` merges every mech-static root it is given into the §7 table. Passing `v9-p1-b` together with `v9-p2-a` would pool their episodes in the go/no-go table; the P2 verdict is unaffected. The documented invocation (`../v9-p1-a ../v9-p2-a`) is correct. Guarding this would change the pinned metrics hash (and so the addendum), which is not worth it before the run.
4. **mech-evolve** now also starts from this W0, with 5 rules disabled. The addendum states this. It matters only for a later confirmatory design, not for b2.
5. **b2 not started.** The user asked to run b after the fix. The paid steps need `FIREWORKS_API_KEY`, which this review never reads, and the root was not pre-created, so a later code change cannot leave a stale copy. Commands (from the plumbing; `$PY` = `../adapter-comparison-work/.venv/bin/python`):

```
$PY -B run.py pilot2-root                                                        # $0, creates ../v9-p2-a
(cd ../v9-p2-a && $PY -B data.py prepare && $PY -B preflight.py --offline-only)  # $0
(cd ../v9-p2-a && $PY -B preflight.py)                                           # paid smoke (cents)
(cd ../v9-p2-a && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)          # paid run (pilot-1 b ~ $5)
# after completion, seal and grade:
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p2-a --json ../v9-p2-metrics.json
```

### Files changed by this review

None. The only file written is this report. Scratch outputs (the selection re-run and a dry-run root) are in the session scratchpad, outside the project.
