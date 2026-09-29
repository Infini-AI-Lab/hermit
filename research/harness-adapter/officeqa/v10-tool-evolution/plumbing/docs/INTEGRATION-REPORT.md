# v10 plumbing — integration report (2026-09-29)

Tree: `officeqa-pro133-v10-plumbing/` (P). Inputs: the W0 scratch copy `officeqa-pro133-v10-w0work/` (W; its
`W0-BUILD-REPORT.md` §3 copy list, §5 controls note), `officeqa-v10-design/DESIGN.md` draft 2a, `build/BUILD-REPORT.md`
("Known gaps"), `PREREG-v10.draft.md`, `PROTOCOL.md` §7–§8. No paid model call was made; no answer key, grader,
`evidence/` or `private-evaluation/` content was opened; no answer value appears in this file or in any file written
here. The v9 trees, `v9-p*`, `v8*`, `frameworks-*`, `opus-officeqa-133` and `officeqa-v10-analysis/` were not modified.

## 中文摘要（写给初学者）

这一轮是"集成轮"：把 W0（起点适配器：建议文字 + 三个表格工具 + 运行时控制）最后一次修补从草稿目录合进正式代码树，
应用两条控制层改动，重新做零成本的回放检查，写好预注册文件，创建五个实验根目录并做离线就绪检查。几个词：

- **回放（replay）**：把 v9 试点里模型敲过的 3,235 条命令重建成 724 条工具调用，用同一套 W0 工具跑两遍比较输出。
  这次发现 1 条 `printings` 的"没找到"提示每次运行都可能不一样（Python 的集合遍历顺序随进程的哈希种子变），修好后
  重跑：724 条重建、0 条变化。
- **硬拦截回显（hard-block echo）**：模型反复敲同一条命令、输出也相同时，运行时插入"已经给你看过了"的提示，现在会附上
  那次输出的前 20 行（≤ 2,000 字节），并且每次重复都提示（以前只提示一次）；循环提示也改成每多 3 次再提示一次。
- **PREREG（预注册）**：`PREREG-v10.md`，除了两处（校准结果、根目录 e 是否跑）都已填好；代码用固定名字 `PREREG.md`
  读它（在本树里是指向 `PREREG-v10.md` 的符号链接）。付费校准跑完、两处填好之后由所有者取哈希。
- **五个根目录**：`../v10-r1-a`…`-e`，每个是本树的一份拷贝加上自己的 `.task-paths.json`；离线就绪检查（$0）的结果见 §5。

接下来要花钱的步骤（需要所有者批准和密钥）见 §6。

## 1. What was merged (task 1, W0-BUILD-REPORT §3)

| file | action |
|---|---|
| `packs/officeqa/tools/otables.py` | W's whole file (the 33-line diff: `_cut_block`, the `_assemble_find` loop fix, `RESERVED_BLOCKS = 3`; the unused `SHIFT_SIBLINGS` constant that P still carried is gone with it), then **one integration fix** on top: `_period_range` orders labels that tie on the (year, month, day) key by their text (§3) |
| `packs/officeqa/tools/cli.py` | identical already; copied |
| `packs/officeqa/observe.py` | kept P's copy (the only diff was P's own `.kit` comment) |
| `packs/officeqa/pack.py` | the F8 phrase "retry with fewer words." was already in P; only the byte-count comment hunk applied (reworded: the proposal now carries the amendment); P's exposure-record block kept |
| `tests/test_officeqa_tables.py` | W's whole file (+11 `test_fix_*` tests) plus one new test for the `_period_range` tie order |
| `tests/test_officeqa_pack.py` | `PENDING_OPTIONS = set()`, the `_proposal_blocks` amendment loop, docstrings; `"harness": 4053` was already there; `SEED_AMENDMENTS = []` because `officeqa-v10-analysis/synthesis/W0-PROPOSAL.md` §(a) already reads "retry with fewer words." (verified by grep: line 303); the drift test compares the SEED texts byte for byte against the amended proposal |
| `tests/test_v10_arms.py` | W's `Broken` (replaces `both with python,` → `both with calc,  `, same size) and `Short` (`[:2500]`) helper cases; `Missing` was already identical |
| `packs/officeqa/seed_notes.json` | already deleted in P |
| `adapters/harness-w0`, `adapters/prompt-w0` | regenerated with `arms.py build`; `arms.py check` passes; `adapters/harness-w0/tools/{cli.py,otables.py}` byte-identical to `packs/officeqa/tools/`, `calc.py` to `harness_core/tools/calc.py` |
| `docs/w0/W0-CONTENT.md`, `docs/w0/W0-BUILD-REPORT.md`, `docs/w0/REPORT.md` | copied from W (`W0-CONTENT.md`, `W0-BUILD-REPORT.md`, `w0replay/REPORT.md`) for the record |

`arms.py` CLI: `--pack` now has `choices=PACKS` and refuses `build`/`check` of a non-officeqa pack into the tree's
`adapters/` without `--adapters DIR` (`W0_NAMES` are pack-independent, so `build --pack toy` would have overwritten the
officeqa W0); the toy E2E path (`--pack toy --adapters <tmp>`) is unchanged. Test:
`test_cli_refuses_another_pack_into_the_tree_adapters`.

Tests after the merge: `tests/test_officeqa_tables.py` (fast + `V9_SLOW=1` slow tier) + `test_officeqa_pack.py` +
`test_v10_arms.py`: 151 passed.

## 2. Controls behaviour change (task 2, W0-BUILD-REPORT §5 items 1 and 2 only)

`controller_runtime.py` (`MechController._loop_guard`, `_hard_block`, new `_cache_output_heads`, `_repeated_output`,
`output_head`, `output_body_text`; constants `HARD_BLOCK_ECHO_LINES = 20`, `HARD_BLOCK_ECHO_BYTES = 2_000`;
`HARD_BLOCK_TEMPLATE` = `blocked: identical output already shown for: {cmd} (call {call}, {repeats} repeats)\n{head}{options}`):

1. **Re-noting**: `self.noted[cmd]` holds the count at the last note; a command is noted again when
   `count - noted >= loop_threshold` (same `_loop_text`, `loop_notes += 1`, decision `loop_note`); gate on and off.
2. **Hard block on every identical repeat, with the echo**: gate off, once `loop_notes >= loop_notes_max`, every
   further issue of a *noted* command whose latest output sha equals an earlier issue's sha gets the block
   (`self.blocked[cmd]` = count at the last block, so a re-sent request never blocks twice); the note names the call
   that first showed the output and the number of issues that printed it, echoes the first 20 lines (≤ 2,000 B) of
   that output (cached per call id from the `function_call_output` items in `on_request`, after the ledger ingested
   them) and ends with `LOOP_OPTIONS_NOGATE`. A repeat whose output changed falls back to rule 1. Decision rows carry
   `repeats`, `first_call`, `echo_bytes`.
3. `empty_output_retry` (item 3): **not built** (deferred, as instructed). `controls.json` values are unchanged
   (the design's defaults).

Byte accounting: `arms.harness_fixed_text(False)` now also records `hard_block_echo_cap` = 2,000 (never counted:
`arms.RECORDED_ONLY` = the gate frame + the echo cap; `model_visible` and `learner_common.controller_template_bytes`
exclude the same keys; the equality test `test_controller_templates_are_counted_per_gate_and_pinned_to_arms` pins
both and the new key). Counted gate-off templates: 72 + 87 + 62 + 99 = **320 B** (was 313 B before the longer hard-block
template); harness arms' model-visible total 6,885 B.

Tests: `tests/test_v10_broker.py` — `test_gate_off_loop_note_wording_escalation_and_hard_block` (updated: the 5th
identical `cat a` is blocked again), new `test_loop_note_repeats_every_threshold_further_issues` (gate off and on;
`loop_threshold` 3 and 4), new `test_gate_off_hard_block_echoes_the_output_head_and_blocks_every_identical_repeat`
(30-line output cut to 20 lines / ≤ 2,000 B; `(call 1, 4 repeats)`; changed output → re-note; `output_head` /
`output_body_text` edge cases), new CodexSim test `test_gate_off_re_note_and_hard_block_echo_with_codexsim` (notes at
the 3rd and 6th `cat a`, blocks at the 4th and 5th `cat b` with the echo, counts per request body, summary counters);
`tests/test_v10_codex_live.py::test_loop_note_gate_off_wording_then_hard_block` (the pinned Codex binary: counts
`[0, 0, 0, 1, 2, 3]`, both blocks echo `same`); the toy E2E `test_gate_off_loop_notes_then_hard_block` is unchanged in
behaviour (3 notes, 1 block) and passes. `PROTOCOL.md` §3.4 records the change.

## 3. Replay preflight (task 3)

`preflight.py --replay-v9a ../v9-p1-a` (read-only evidence root; source = candidate = the merged W0 tools; the
determinism check):

| run | index | commands | rebuilt | exit 0 | changed | timed out | wall |
|---|---|---|---|---|---|---|---|
| first (merged W0) | v9-a root's own (v9.1) | 3,235 | 724 (285 `find_tables`, 274 `show_table`, 165 `printings`) | 721 | **1** (`printings`, episode 021 step 15) | 0 | 143.7 s |
| after the fix | v9-a root's own (v9.1) | 3,235 | 724 | 721 | **0** | 0 | 141.2 s — **passed** |
| after the fix (the kept record) | the campaign's v10 index (`--replay-tables-index`, new) | 3,235 | 724 | 721 | **0** | 0 | 144.0 s — **passed** |

The one change: the `printings` no-match line "the scanned parts print periods A .. B" chose A among labels that tie
on the period key (`FY1935-1953`, `CY1935-1951`, `FY1935-1951`, all year 1935) in set-iteration order, which follows
Python's per-process hash seed (`-I` python, `PYTHONHASHSEED` unset): three distinct outputs in six runs of the same
argv. `otables._period_range` now sorts by `(_period_sort(p), p)`. This is a deviation from W's `otables.py`
(one line + comment; test `test_fix_period_range_is_deterministic_on_tied_labels`); the adapters were rebuilt and the
W0 revision / tool hashes in PREREG are the post-fix ones. The 3 non-zero exits are the pilot's own recorded exit-2
invocations (identical in both runs). `preflight/replay-v9a.json` replaced (the pre-S4 record is superseded);
`PROTOCOL.md` §6 updated with these numbers.

### 3a. Tables-index finding (fixed; found while preparing the roots)

`data.py prepare` installs `TABLES_SOURCE` = `.task-paths.json["tables_index"]` or, by default, the
**frameworks-v9** index (`otables-index-v9.1`: tables `t`/`f` only). The v10 tools open a v9.1 index without error
(`otables._sections` catches the missing `s`/`fs` tables and returns `[]`), so every root would have run W0-12
(narrative section index) silently disabled, and `verify_prepared` / preflight would not have noticed (they check
the corpus binding, not the version). The v9-a replay above also ran on the v9.1 index in its first two runs, so its
first two passes never exercised the section tables. Fixes:

- `run.py roots` writes `tables_index` (default `<parent>/frameworks-v10/officeqa-tables-index/tables.sqlite`,
  `--tables-index FILE`), refuses a missing file or a wrong version; `study_configs` takes `tables_index`
  (`run.STUDY_TABLES_INDEX`).
- `data.tables_index_version(index)` refuses any version other than `otables.INDEX_VERSION`;
  `install_tables_index` calls it; `verify_tables_index` returns `version` and `sections`.
- `preflight.offline` checks `tables_index_version_current`; `preflight.py --replay-v9a` gains
  `--replay-tables-index FILE` (the replay above was re-run with it; the record carries the index and its version).
- `calibrate.py` gains `--tables-index FILE|none` (default: the study index when it exists): `link_prepared`
  links the source root's `prepared/` entry by entry with the study index in place of `agent/tables/tables.sqlite`
  (the v9-a root's own index is v9.1), version-checked and recorded in `calibration.json["block"]`.
- Tests: `test_tables_index_is_bound_to_the_corpus` (+ version refusal), `test_run_roots_creates_the_five_neutral_study_roots`
  (+ `tables_index` recorded, missing / older index refused), new
  `test_scratch_root_serves_the_study_tables_index_in_place_of_the_sources`.
- The five roots had been created before the fix; they were re-synced in place (the same `copytree` + `study_configs`
  as `run.py roots`, verified file by file against the tree) since deleting them was not permitted in this session.

### 3b. One flaky E2E failure (not a regression)

The first full-suite run after the controls change failed `test_v10_e2e.py::test_learners_admit_deterministically_and_serve_the_revision`
once: the toy05 episode's `lookup widgets_2020_a.txt West` completed with exit 0 and **0 output bytes** (the next
lookup returned 242 B; no controller insert was involved) — the Codex PTY capture loss W0-PROPOSAL §(e) item 3
(`empty_output_retry`, deferred) describes ("5 capture losses, 0 grade effect in the pilot"). The E2E file passed on
its own before (39/39 with codex_live) and after (22/22) that run.

## 4. PREREG (task 4)

`PREREG-v10.md` written from the draft plus PROTOCOL §8: Chinese summary; roots; contrasts with the exposure
record (`pack.exposure_record()`: 12 changes, 45 targeted ids = 45 of the 50 subset questions; the 5 untargeted are
UID0019, UID0030, UID0096, UID0230, UID0240 — stated plainly); metric (`vendor/officeqa/reward.py` sha256
`0d91698c…8138f`, hashed without opening it), noise band, go/no-go; every $0 value: subset ids and sha
`c95f9613…a07d`, the 5 issue-47 ids, `order_seed`, repeat, T = 0, no top_p, `ACTOR_SECONDS` 3,600, Codex version and
sha256 `9280c075…d1f9`, both W0 revisions (`02948081b850` harness, `720b7ba30245` prompt), per-file tool hashes and
bytes, SEED hashes (4,053 / 4,092 B), USAGE 2,512 B, model-visible totals (0 / 4,092 / 6,885), the 13-item parity
checklist, `controls.json` + `CONTROLS_RANGES`, the controls change of §2, learner constants, ToolRunner profile and
constants, screen version `aceb0ce91240` with the banned lists, replay thresholds and result, `build_digests.py` sha256
`a2643477…af89`, the residual exposures (Known gaps 4, 5, 7), the tool-process and model-visible-name statements, the
d-vs-e one-factor claim, the b2 = **26**-question caveat (`v9-p2-a/state.json`: 26 results, 26 episode directories;
the design documents say 25). Exactly two `[FILL AT FREEZE]` slots remain, each also tagged `[DECISION PENDING]` so
`preflight.py` lists them: the calibration outcome (§6) and the launch-time decision about root `e` (§1). **Not
hashed** (the owner hashes after the paid calibration).

File name: the code (`campaign.CODE_FILES`, `preflight`, `run.py`, `full_audit`) reads `PREREG.md`; in P that is a
symlink to `PREREG-v10.md`. `run.py roots` copies with `symlinks=False`, so every root holds one regular `PREREG.md`
(`PREREG-v10.md` is in `COPY_IGNORE`); the roots test asserts it.

## 5. The five roots (task 5)

ROOTS-SECTION

## 6. Exact next steps for the owner (paid; each needs your approval and the Fireworks key)

```
PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python
B=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd
P=$B/officeqa-pro133-v10-plumbing
```

1. **Calibration** (one harness-learner session on the v9-a evidence, ≤ 20 actor runs; writes
   `$P/preflight/calibration.json`; the scratch root serves the v10 tables index by default, §3a):
   `cd $P && $PY -B calibrate.py --source-root ../v9-p1-a --actor-runs 20`
   Then copy the record into the roots (roots do not copy `preflight/`; `preflight.offline` of a harness-learner root
   requires it): `for r in a b c d e; do mkdir -p $B/v10-r1-$r/preflight && cp $P/preflight/calibration.json $B/v10-r1-$r/preflight/; done`
2. **Fill the two `[FILL AT FREEZE]` slots** in `$P/PREREG-v10.md` (calibration outcome; root `e` decision), remove
   their `[DECISION PENDING]` tags, then copy the document into every root under the code's name:
   `for r in a b c d e; do cp $P/PREREG-v10.md $B/v10-r1-$r/PREREG.md; done`
   (`sha256sum $P/PREREG-v10.md` is the hash preflight will record.)
3. **Readiness smoke per root** (paid, cents; records the PREREG hash and writes `preflight/readiness.json`):
   `for r in a b c d e; do (cd $B/v10-r1-$r && V9_SLOW=1 V9_LIVE_CODEX=1 $PY -B preflight.py) || break; done`
   (drop `e` from the loop if step 2 decided it does not run; the barrier's roots list in every `.task-paths.json`
   names five roots — recreate the roots with `run.py roots --arms plain prompt-evolve harness-static harness-evolve`
   in that case, then repeat step 3.)
4. **The round**: start the roots together (the barrier pairs their blocks):
   `export FIREWORKS_API_KEY=…; for r in a b c d e; do (cd $B/v10-r1-$r && nohup $PY -B run.py run > campaign-stdout.log 2>&1 &); done`
   Cost after each block: `(cd $B/v10-r1-$r && $PY -B run.py cost | head -c 800)`; stop with SIGINT to the pid in
   `process.json`; `run.py resume --clear-abort` continues at the block level. After the round:
   `run.py audit && run.py cost && run.py report` per root ($0).

## 7. Test count

FINAL-TESTS-LINE

## 8. Open items

- The paid calibration and the readiness smokes (steps 1 and 3) have not run; `preflight/calibration.json` in P is
  the $0 dry-run record (`dry_run: true`).
- `docs/w0/REPORT.md` §9 open items of the W0 fix pass stand (the proposal's §1.2 rank numbers, 4 right-cell
  `find_tables` pages that lose the v9 top group, the `w0replay` artefacts living in a session scratchpad).
- The residual exposures of PREREG §7 (leak screen best-effort, broker body shape, replay cwd parent) are unchanged.
- `empty_output_retry` (W0-PROPOSAL §(e) item 3) is deferred; DESIGN scope decisions 1–8 stand.
