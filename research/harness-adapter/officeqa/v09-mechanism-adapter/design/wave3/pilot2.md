# v9 wave 3: re-pilot b2 prepared (PREREG addendum P2): 2026-09-29

## 中文摘要（给用户）

**结论：** 你批准的两处修复都已经改完：只修 b，然后只重跑 b。全部测试通过（435 passed；原来 419 个，新增 16 个）。
这一步**没有任何付费调用**，也没有访问 127.0.0.1 以外的网络。四个 pilot-1 目录完全没动：改动前后我都对文件列表、大小和修改时间做了快照，两次一致。

新的 root `v9-p2-a` **还没有创建**：
- 这一步的规则是"不做付费调用"。
- 如果之后还有复查要改代码，提前建好的副本就会过期。

要"跑 b"，下面第 5 节的命令可以直接用。前三步免费，后两步付费，需要 Fireworks 密钥。

**先解释几个词：**
- **W0（第 0 版适配器）**：每个实验臂开跑时拿到的初始"知识包"。
  - text 臂的 W0 是一段建议文字，文件是 `SEED.md`，最后会拼进模型读到的 `AGENTS.md`。
  - mech 臂的 W0 是：文字 + 规则文件 `rules/*.json` + 开关文件 `policy.json`。
- **gate（提交闸门）**：模型运行 `submit --answer "…"` 时，harness 用规则检查这个答案。
  - 有问题就回一条 **finding（发现）**，可能附带一个 **Offer（替代答案）**。
  - 模型可以 **Accept（接受）** 这个替代答案，也可以 **Keep（坚持）** 原答案。
- **`--alt`**：`submit` 的一个参数，意思是"我还考虑过另一个值"。
  - 规则 `competing_candidates` 会把这个值原样当作 Offer 还给模型。
  - pilot 1 里两次"对改错"都来自这里：模型本来答对了，接受了自己的备选值，最后答错。
- **engine precision（规则精度）**：某条规则触发的 episode 里，当时的候选答案**确实是错的**所占的比例。
  - 越高越好：说明这条规则主要在错的时候才报警。
  - 算法完全照 `pilot/metrics.py` 的逐规则表。
- **union fire rate on right episodes（对题上的合并误报率）**：在"答对的题"里，至少有一条仍启用的规则触发的比例。
  - PREREG §5 规定这个比例不能超过 20%。
- **replay / recheck（回放 / 复核）**：拿 pilot 1 存下来的每次闸门检查的**全部输入**，离线重新计算一遍。
  - 结果和当时在线的结果一模一样，说明回放可信。简称"线上 = 线下"。
- **addendum（补充条款）**：PREREG 已被哈希锁定，之后的任何改动只能追加一段带日期的补充条款。
  - 条款里的 **pin（钉住）** 指写进去的 sha256 哈希，代码或文件一变就对不上。

**改了什么（完整清单见英文第 2 节）：**
1. **mech 的 W0 = text 的 W0 + 收尾说明。**
   - `adapters/mech-w0/SEED.md` 的前半部分，就是 `adapters/text-w0/SEED.md` 的逐字全文（2,478 B）。
   - 中间空一行，后面接"如何用 submit 收尾 / finding 与 Offer 是什么 / Accept 与 Keep / `[harness:...]` 行"这段说明。
   - 这样规则是**加在**建议之上，而不是**替换**建议。
   - 所有模型能看到的地方都不再提 `--alt`：SEED、README、闸门的用法行、`submit --help`。命令行仍然接受这个参数。
2. **按预注册程序关掉 5 条规则**，写进 `policy.json` 的 `disabled_rules`。规则代码和阈值都没改。
   - 关掉的：`competing_candidates`（不论数据如何都关）、`table_alternatives`、`grounded`、`row_coverage`、`flagged_cells`。
   - 保留的：`recompute_final`、`scale`、`later_printing`、`answer_count`、`period_match`。
3. **PREREG 补充条款 P2**（`pilot/ADDENDUM-P2.md`），已逐字追加在 PREREG.md 末尾。
   - PREREG 新哈希 `4d550956…7d9e`。pilot-1 的旧哈希是 `baa53c64…2fa3`，前面的正文一个字没改。
4. **`run.py pilot2-root`**：建一个中性名字的 root `v9-p2-a`。
   - 如果缺少 P2 条款，或条款里钉住的哈希和代码对不上，它会拒绝执行。
   - `pilot/metrics.py` 新增了"b2 对 a"的判定，P1 的温度规则代码没动。
5. 测试：新增 `tests/test_v9_pilot2.py`（16 个），并改了 3 个已有测试。这 3 个原本写死了旧设计。

**规则选择的过程**：在 pilot-1 答对的 21 道题上，看仍启用的规则有几道会报警。

| 步骤 | 关掉 | 对题报警 | 可归因的"对改错" |
|---|---|---|---|
| 0 | 无 | 12/21 = 57.1% | 2 |
| 1 | `competing_candidates`（强制） | 10/21 = 47.6% | 0 |
| 2 | `table_alternatives`（精度 0.50） | 9/21 = 42.9% | 0 |
| 3 | `grounded`（0.60） | 6/21 = 28.6% | 0 |
| 4 | `row_coverage`（0.67） | 5/21 = 23.8% | 0 |
| 5 | `flagged_cells`（0.71；与 `competing_candidates` 同精度、同对题次数，已先被关） | 3/21 = 14.3% ✅ | 0 ✅ |

- **复核：** 86 次闸门检查全部离线重算一致。只用保留规则重算时，结果也等于"原记录去掉被关规则"，86/86。

**⚠ 一个你需要知道的敏感性问题：**
- 上面用的是 P1 事先指定的定义，也就是 `pilot/metrics.py` 的逐规则表。
- 换两种同样说得通的定义，关掉的集合会不一样：
  - 用"最终答案的官方成绩"做标签：关 `competing_candidates`、`grounded`、`recompute_final`、`scale`、`table_alternatives`；
  - 只看第一次提交：关 `competing_candidates`、`grounded`、`recompute_final`、`table_alternatives`。
- 三种定义都会关掉的只有三条：`competing_candidates`、`grounded`、`table_alternatives`。
- 分歧落在那些对题上只报过 1 到 3 次的规则。这说明 21 道对题太少，排序很不稳。
- 所以 b2 的结果只能说明"这一套规则 + 完整建议"的效果，不能说明哪一条规则好或坏。

**字节数（模型能看到的适配器文字，预算 16,384 B）：**

| | pilot 1 | P2 |
|---|---|---|
| text W0 | 2,478 B | 2,478 B（不变） |
| mech W0 总计 | 2,429 B | 4,845 B |
| 其中 SEED.md | 829 B | 3,245 B |
| 其中规则模板 | 1,130 B | 1,130 B（被关的 5 条规则的模板 532 B 仍计入，偏保守） |
| 其中控制器模板 | 470 B | 470 B |
| 闸门框架（单独记录，不计入预算） | 275 B | 257 B（用法行去掉了 `--alt`） |

**P2 决策规则（跑之前就写死的）：** 比较 b2 与 pilot-1 的 `text-static` a（31/50）的严格答对数。
- **b2 ≥ 31**：开始规划正式研究，mech = text + 规则；
- **b2 ≤ 27**：规则在 OfficeQA 上没有增益。停掉 OfficeQA 的机制路线，转去第二个 benchmark；
- **b2 = 28–30**：看不出结论。先把 text-static 和 mech-static **同时并排**再跑一次，再做决定。
- 同时照常报告 §7 的 1–5 条 go/no-go 指标。

**注意：** a 和 b2 不是同时跑的。服务商那边可能有漂移，而且同一配置跑两次，约 27% 的题目结果会不同。
- 放到 50 题上，大约有 13 道题会变。答对数之差的标准差约为 3.6 题。
- 所以 ±4 题以内的差距仍属噪声。这条规则只用来决定下一步做什么，不是统计检验。

**下一步（跑 b2）：**
- 先建 root → 准备数据 → 离线检查。这三步免费。
- 再跑付费的就绪 smoke（几美分），最后正式跑。
- pilot-1 的 b 花了大约 $5（已知下限），b2 预计差不多。
- 需要你的 Fireworks 密钥。它不会被写进任何文件。

---

## 1. What was asked and what was done

User request (relayed): "可以，改完跑b" (OK: make the changes, then run b). The computed task for this step: fix b
(mech W0 = text W0 + submit section; rules by the pre-registered §7 procedure; addendum P2; a single-root
command; tests), run the full suite, and make no paid call. All five items are done. The paid re-pilot has not
been started, and the root has not been created (§5).

Pilot-1 inputs (read-only): `v9-p1-a` text-static T=0 31/50, `v9-p1-b` mech-static T=0 20/50, `v9-p1-c`
text-static T=1.0 / top_p 0.95 24/50. `v9-p1-metrics.json`: NO-GO; temperature verdict T=0.

## 2. Every change (plumbing `officeqa-pro133-v9-plumbing/`)

| file | change |
|---|---|
| `arms.py` | `text_seed(pack)`; the mech W0 `SEED.md` = `text_seed(pack) + '\n' + MECH_SEED`; `MECH_SEED` no longer suggests `--alt`; `W0_DISABLED_RULES = {'officeqa': (5 ids)}` and `w0_policy(pack)` (= `POLICY_DEFAULTS` + `disabled_rules`); `w0_files` refuses unknown disabled ids; comments / docstring. The toy pack keeps the defaults (no pilot data there) |
| `adapters/mech-w0/SEED.md` | regenerated: text W0 `SEED.md` verbatim + blank line + finishing section (3,245 B) |
| `adapters/mech-w0/policy.json` | regenerated: `disabled_rules` = the 5 selected rules (rule files untouched) |
| `campaign.py` | `SUBMIT_README` usage line without `[--alt "<other value>"]` |
| `harness_core/gate.py` | `USAGE` (printed on a rejected submit) without `[--alt "<other>"]` |
| `harness_core/tools/submit.py` | `--alt` kept, `help=argparse.SUPPRESS` (not in usage or `--help`); docstring usage line without it |
| `pilot/select_rules_p2.py` | **new**: the pre-registered rule-selection replay (below) |
| `pilot/pilot2-rule-selection.json` | **new**: its output on `v9-p1-b` (counts, rates and rule ids only; no task id, ordinal or answer) |
| `pilot/metrics.py` | the P2 decision rule: constants `P2_*` (incl. `P2_W0_REVISION`), `p2_rule`, `render_p2`, `w0_revision` per root, `result['p2_rule']`; P1 code paths unchanged (re-run on a, b, c gives the identical JSON apart from the two added keys) |
| `run.py` | `pilot2-root` command: `P2_*` constants, `pilot2_pins`, `pilot2_addendum`, `pilot2_config`, `pilot2_root`; `pilot-roots` untouched |
| `pilot/ADDENDUM-P2.md` | **new**: addendum P2 |
| `PREREG.md` | addendum P2 appended verbatim after P1 (the pilot-1 file + P2; nothing above it changed) |
| `PROTOCOL.md` | arms table row, commit-gate usage, seed-rule policy, L5 W0 paragraph and byte counts, §4 pointer to b2 |
| `pilot/README.md` | files table + "Re-pilot b2" section |
| `tests/test_v9_pilot2.py` | **new**, 16 tests |
| `tests/test_v9_orchestration.py` | `test_committed_w0_equals_generated_and_fits_the_budget`: mech seed = text seed + `MECH_SEED`, no `--alt`, policy = defaults + disabled list, the new parity arithmetic (replaces "totals differ by < 1 KB", which encoded the old W0) |
| `tests/test_v9_pilot.py` | pin test: P1's `pilot-metrics-script-sha256` is checked against the pilot-1 roots' copies (they all hash to `dc302abc…`); P2 pins the current `metrics.py` |
| `tests/test_v9_e2e.py` | `test_seed_knowledge_reaches_the_model_as_decided`: mech arms now also carry the working advice; no `--alt` in any AGENTS.md |

Not changed: rule files, rule code, predicates, offers, thresholds, `contracts.py`, the text W0, the pilot-1 roots,
v8 dirs, `frameworks-*`, `MetaHarnessAdapter*`, `opus-officeqa-133`.

## 3. Rule selection (PREREG §5 / §7 item 4; `pilot/select_rules_p2.py`)

Inputs: `v9-p1-b/runs/episodes/*/api/checks/*.json` (86 checks, 50 episodes), `api/ledger.jsonl`, the sealed
final answers. Labels come from the strict official `reward.py`, applied controller-side after the seal; they
reproduce `private-evaluation/mech-static-grades.json` exactly, and the script refuses otherwise. The
per-rule table must equal `pilot/metrics.py`'s own table (checked).

Definitions (the P1 operational definitions):
- **right episode**: its judged candidate is right. Here that is the first checked candidate in all 50
  episodes, so there are 21 right episodes.
- **engine fire of rule r on the right side**: r is in the `fired` list of a check whose candidate is right.
- **attributable right→wrong flip**: r was shown in that episode, or r fired on its pre-finding candidate.

Procedure:
1. Disable `competing_candidates` first.
2. Then disable the lowest engine precision first. Ties go to more right-side fires, then to the rule id.
   Never-fired rules come last and are not disabled.
3. Stop when (a) the union of right-side fires is ≤ 20% of the right episodes and (b) no right→wrong flip is
   attributable to an enabled rule.

| rule | right / wrong fired | precision | shown in | attributable r→w |
|---|---|---|---|---|
| table_alternatives | 2 / 2 | 0.500 | 4 | 0 |
| grounded | 4 / 6 | 0.600 | 8 | 0 |
| row_coverage | 1 / 2 | 0.667 | 2 | 0 |
| competing_candidates | 2 / 5 | 0.714 | 6 | 2 |
| flagged_cells | 2 / 5 | 0.714 | 6 | 0 |
| recompute_final | 3 / 8 | 0.727 | 6 | 0 |
| later_printing | 0 / 1 | 1.000 | 1 | 0 |
| scale | 0 / 2 | 1.000 | 2 | 0 |
| answer_count, period_match | 0 / 0 | n/a | 0 | 0 |

Steps: see the Chinese table. The limits first hold after `flagged_cells` (3/21 = 14.3%, 0 attributable).
Without the forced first step, the ordered procedure reaches the same set: `competing_candidates` sorts before
`flagged_cells` on the id tie-break.

Recheck: 86/86 stored checks re-derive identically with `harness_core.replay.recheck`, with offers re-executed
in the sandboxed runner. The enabled-only re-evaluation equals the stored fires restricted to the enabled rules
on 86/86.

Projection on the pilot-1 trajectories:
- engine fires of the 5 enabled rules on right episodes: 3/21 (14.3%);
- on wrong episodes: 9/29 (31.0%). On the first check only it is 8/29 (27.6%), and criterion 2 (wrong ≥ 2 ×
  right = 28.6%) would then just miss.

This is not a b2 prediction: how the model replies to a finding cannot be replayed.

Sensitivity (reported in the JSON, not used):

| labels | disabled rules |
|---|---|
| final-grade | competing_candidates, grounded, recompute_final, scale, table_alternatives |
| first-check | competing_candidates, grounded, recompute_final, table_alternatives |

- The final-grade labels count a successful correction as a false alarm. `scale` fired on the one wrong→right
  flip.
- The first-check labels ignore fires on re-submissions.
- All three definitions agree only on `competing_candidates`, `grounded` and `table_alternatives`.

## 4. Addendum P2 and the new command

`pilot/ADDENDUM-P2.md`, appended to PREREG.md, contains:
- the pilot-1 results (counts only);
- both changes with their justification, the rule table and steps, the recheck and the sensitivity;
- the re-pilot design (one root `v9-p2-a`: mech-static with the new W0, pilot-first50, repeat 1, order seed
  `v9-officeqa-pro133-order`, T=0, no top_p, barrier `v9-p2-barrier` over this root only, 43,200 s);
- the decision rule: b2 ≥ 31 proceed, b2 ≤ 27 stop, 28–30 inconclusive → a paired concurrent re-run;
- the re-report of the §7 criteria for b2;
- the caveat: different times, provider drift, ~27% run-to-run flips, SD ≈ 3.6 questions on 50.

Machine-checked lines (`p2-…`):
- fixed values (root, arm, T, top_p, repeat, order seed);
- byte counts (text 2,478; mech 4,845; mech SEED 3,245; gate frame 257);
- decision numbers (reference 31, proceed ≥ 31, stop ≤ 27);
- pins:

| pin | sha256 |
|---|---|
| mech W0 revision | `d4e9c3a1a574fca83ab8c25a71babe5fd0393f68346f2292d845bdc5a9102398` (pilot 1: `70fc8979…`) |
| `adapters/mech-w0/SEED.md` | `8a331deac25fb552cc3c26b0dc17774b87504abcf4d8d15c2c4b455061fc623c` |
| `adapters/mech-w0/policy.json` | `0349180f10227f971d701542c92f355b9f21eeb1aaaa26f67f9bb4fd15fb658a` |
| `pilot/pilot2-rule-selection.json` | `bddd065ab52af7416e9a7dc89cbb36eddf301ca67d197318ba0b0753efba19e8` |
| `pilot/select_rules_p2.py` | `c26046e611c743891b050e454242d4a158bf6f3411fe24ac53e5cf750da9badf` |
| `pilot/metrics.py` | `156c4d533bb4d163f0450f73b7f6dc4d1e52e66923c60f3c6b178e3ebf63dd7e` |
| PREREG.md (P1 + P2) | `4d550956c88e4c6736c2df746d9d713673eb19e6610813ae9a28b6160a407d9e` |

`run.py pilot2-root [--parent DIR]` refuses in these cases:
- P1 or P2 is missing, or P2 is not appended verbatim after P1;
- a `p2-` line is duplicated, or a fixed value or pin differs from the plumbing;
- the selection's disabled list ≠ the W0 `policy.json`;
- the committed W0 ≠ the generated W0;
- it is run inside a root;
- the root or barrier already exists.

`pilot/metrics.py ../v9-p1-a ../v9-p2-a` works as follows:
- It picks b2 by its frozen W0 revision, so a pilot-1 mech root is never taken for b2.
- The reference must be text-static at T=0 without top_p, with the pinned 31 right.
- It prints the §7 table for b2 and `P2 VERDICT: …`.
- The exit code stays the go/no-go result.

## 5. Tests and verification

- Full suite: `V9_SLOW=1 V9_LIVE_CODEX=1 …/.venv/bin/python -B -m pytest -q -p no:cacheprovider tests` gives
  **435 passed** in 330 s. The baseline before the changes was 419 passed.
- Pilot-1 roots: the metadata snapshot (paths, sizes, mtimes) of `v9-p1-a`, `-b`, `-c` and `-barrier` is
  identical before and after, and `v9-p1-metrics.json` is unchanged.
- Hygiene: no answer value was printed or written, no network call beyond 127.0.0.1, no key or credential file
  was read.

Commands to run b2. Steps 1–2 are $0; steps 3–4 are paid and need `FIREWORKS_API_KEY` or the hidden prompt.

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
cd /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-v9-plumbing
$PY -B run.py pilot2-root                                                             # 1. creates ../v9-p2-a
(cd ../v9-p2-a && $PY -B data.py prepare && $PY -B preflight.py --offline-only)       # 2. ~6 min
(cd ../v9-p2-a && $PY -B preflight.py)                                                # 3. paid smoke, cents
(cd ../v9-p2-a && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &)              # 4. paid, ≈ $5 (pilot-1 b)
# after completion + seal + grade:
$PY -B pilot/metrics.py ../v9-p1-a ../v9-p2-a --json ../v9-p2-metrics.json
```
