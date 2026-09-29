# v10 plumbing — build report, fix pass (build round 2, 2026-09-29)

Tree: `officeqa-pro133-v10-plumbing/` (the v9 trees, the v9 pilots, `frameworks-*` and `opus-officeqa-133` were not
touched). Design: `officeqa-v10-design/DESIGN.md` draft 2a; review: `review/DESIGN-REVIEW.md`. No paid model
call was made; no answer key, grader, `evidence/` or `private-evaluation/` content was printed or copied
(the $0 calibration dry run reads the v9-a answer key programmatically through `data.release_references`, as a
campaign does, and writes counts only).

## 中文摘要（写给初学者）

这一轮是"修补轮"：审稿人对上一轮代码提了 20 条意见（8 条 major，12 条 minor），我逐条修了，并把一直缺着的
"W0 内容"（S4：新的建议文字、改好的工具、曝光记录）合进了正式代码树。几个关键词：

- **paired re-run（配对重跑）**：学习器提出的修改，要在"它本该修好的题"和"原本答对的对照题"上各重跑两次才采纳。
- **ToolRunner（工具回放沙箱）**：学习器改了工具后，控制端先用旧工具和新工具把过去记录过的命令各跑一遍做比较，这一步是
  零成本的筛查（不调用付费模型）。
- **model-visible（模型可见）**：actor 模型在工作目录里能看到的任何路径或文字。它绝不能透露"你在哪一组实验里"。

修了什么（按重要性）：
1. **验证重跑的目录名会泄露信息**：以前 actor 的工作目录是 `evolution/03/verify/<学习器起的名字>/q007-k1/workspace`，
   名字里可能藏着答案，`evolution` 一词也说明"正在学习"。现在改成 `runs/verify/03/r01/01/workspace`：`r01` 是不透明的
   编号，映射表只存在控制端；提案的 id 无论带不带笔记都过泄漏筛查；审计（full_audit）会检查每个验证目录名是中性的。
2. **回放沙箱和真实 actor 环境长得不一样**（工具代码可以借此"在回放时装乖"）：现在回放从 `runs/code/<哈希>/tools`
   的中性副本运行，环境变量的键和 actor 一样，当前目录也叫 `workspace`，只放行脚本的 import 闭包文件；禁止启动子进程的
   调用（`os.fork` 等）在筛查阶段就被拒绝；还加了 3 GB 内存上限（macOS 不支持 `ulimit -v`，用轮询看门狗实现）。
3. **回放只看最近 80 条命令，支持题的命令可能不在里面**：现在先放支持题的所有命令，再补最近的其它命令。
4. **`expect` 写 OUTPUT 行的步骤号被误判为"无关"**：现在两种步骤号都接受，统一换算成 COMMAND 步骤号。
5. **标识符泄漏筛查太严**（`text`、`note` 这类普通变量名会被拒）：只有"这个文件里从未出现过的"暴露词才算泄漏。
6. **d 组和 e 组看到的证据不一样**：两个学习器现在共享同一段"工具证据"提示、同样的只读文件；区别只剩四个"改工具"的工具。
7. **审计没有把每个 block 用的 adapter 和产生它的学习轮次对上**：现在会对上（含未改工具的轮次的 admission 记录）。
8. **W0 内容（S4）终于进正式代码树**：SEED 文字逐字节等于提案、13 条对照清单、工具改动 W0-1..W0-12、USAGE 文本、
   曝光记录 `W0_CHANGES`（哪道开发题催生了哪项改动）。
9. 小项：数值折叠更聪明（`int('2'+'52')` 也会被抓）、审计重算代码授权、`search_adapter_file` 改成纯文本搜索（防 ReDoS）、
   `if tool …; then` 和 `$(tool …)` 也能回放、只改 USAGE.md 不消耗回放次数、Broker 的 submit 路由默认关闭、`.harness/`
   改名 `.kit/`、`[harness:…]` 改成 `[runtime:…]`、新的 `calibrate.py` 校准入口。

## What was built (by finding)

| # | severity | finding | outcome |
|---|---|---|---|
| 1 | major | note-less revision ids not leak-screened; verification cwd carries the id / `evolution/` | **fixed** — `learner_common.id_leak_findings` runs on every proposal id (notes and revisions: pack screen, question-copy, `LEAK_IDS` incl. upper-cased `uid0007`); verification episodes now run in `<root>/runs/verify/BB/<token>/<NN>/workspace` (`campaign.Layout.verify_runs`, tokens `r01…` recorded in `evolution/BB/verification.json`); `campaign.verification_runner` refuses any other location and any path failing `arms.check_neutral`; `full_audit` checks every workspace path (scored and verification) and that no verification episode lives under `evolution/`; the E2E leak test now walks the verification request bodies (every path under the root neutral, no proposal id, no `q\d{3}`, no `evolution`, and the fragments `harness/evol/mwm/baseline/adapters/` absent from every input). Tests: `test_note_less_revision_ids_are_leak_screened`, orchestration `test_evolve_campaign_…` (runs/verify layout), `test_verification_runner_refuses_…` (revealing path refused), E2E `test_model_visible_text_never_names_the_arm`. |
| 2 | major | replay environment distinguishable from the actor | **fixed** — `campaign.tool_runner(block)` stages a neutral copy under `runs/code/<tools sha12>/tools` (`stage_tools_copy`) and builds the runner through the one constructor `campaign.make_tool_runner`; `harness_core.runner.ToolRunner` uses the actor kit's environment key set (`actor_like_env`: HOME/TMPDIR/TMPPREFIX/ZDOTDIR/PATH/LANG/PYTHON*/MPLCONFIGDIR into the work dir), a cwd named `workspace`, and grants per run exactly the script's import closure (`runner.import_closure`, now the one definition `kit._import_closure` delegates to) plus the tools listing when there are siblings — never the whole tools dir (a runtime read of USAGE.md fails in both). `subprocess`, `pty` and `os.fork/exec*/system/popen/posix_spawn*/spawn*` calls are refused by the screen (`PROCESS_CALLS`), so the prompt's claim is true. `learner_common.tool_runner_for` caches by content sha so a re-staged candidate gets a fresh copy. Tests: E2E `test_replay_environment_matches_the_actor_kit` (a tool branching on HOME / `runs/code` in `__file__` / cwd name prints the same through the replay and the kit wrapper), runner `test_tool_runner_runs_the_toy_w0_lookup_…` (env keys == `kit.tool_environment`, USAGE.md unreadable, `kit.KIT == runner.KIT_DIR`), `test_process_calls_and_modules_are_refused`. PREREG statement in PROTOCOL §8. Residual: the replay cwd's parent is not `runs/episodes/NNN`; fork/exec and the broker port differ by design. |
| 3 | major | replay job window drops the support episode's commands | **fixed** — `replay_jobs(ctx, support)` runs two passes (every invocation of the support episodes first, then the most recent of the others up to `REPLAY_JOBS_MAX`), rows carry `support`; the screen records `expect_coverage` {expected, replayed, not_replayed} in the replay record and says "not replayed" when that is why. Test: `test_replay_jobs_cover_the_support_episodes_beyond_the_window` (`REPLAY_JOBS_MAX` = 3). |
| 4 | major | `expect` given as the OUTPUT step never matches | **fixed** — normalised to the COMMAND step at validation (de-duplicated); the tool description says either line is accepted. Test: `test_expect_accepts_the_output_step_of_a_command`. |
| 5 | major | identifier leak screen refuses ordinary identifiers | **fixed** — delta rule for the arm-revealing words: `text_leak_findings(..., known_words=file_words(source))` refuses only words absent from the source file (identifiers, strings and comments alike); file names, manifest names and the code-root path keep the full `ARM_REVEALING` check. Test: `test_identifier_leak_screen_counts_only_words_new_to_the_file` (a `text = …`/`plain = …` patch into the OfficeQA cli.py passes; `harness_mode` is refused). |
| 6 | major | d vs e not a one-factor contrast | **fixed (option a)** — `learner_common.tools_evidence_section` (taxonomy (a)-(d), manifest, USAGE.md, diff vs W0, W0 change list, friction digest) is built by `build_prompt` for every harness adapter, for both learners; `search_adapter_file` is a common tool; `prompt_learning.SPEC.adapter_reads` includes `tools/*.py`, `tools/USAGE.md`, `tools/manifest.json`, `controls.json`; the harness learner keeps only (e) and the four proposal tools. Test: `test_both_learners_see_identical_evidence` asserts the shared section is byte-identical in both prompts and `requests['notes']['tools'] + [4 proposal tools] == requests['harness']['tools']`. |
| 7 | major | audit never ties the served adapter to the admitting round | **fixed** — `full_audit.Audit.block_adapters`: block 1 = W0; block b adapter = round b-1 candidate when activated else block b-1 adapter; revision = round record = `arms.revision(tree)`; every activated round has a `mechanical-admission.json` naming the candidate revision (note-only rounds included); `learned_text` compares the ledger row's text and triggers with the served note. Test: `test_audit_ties_block_adapters_to_rounds_and_re_derives_code_grants`. |
| 8 | major | W0 content (S4) not in the plumbing | **fixed** — ported the finished S4 work from the scratch copy `officeqa-pro133-v10-w0work` (task X texts, task T tools; its 117 pack/tables tests passed there): `packs/officeqa/pack.py` (HARNESS_SEED / PROMPT_SEED = W0-PROPOSAL §(a)/(b) byte for byte, 4,053 / 4,092 B — the harness text follows the proposal's current wording, `SEED_KNOWLEDGE` K1..K13, `USAGE`, `seed_notes() == []`, new `W0_CHANGES`, `EXPOSURE_ITEMS`, `w0_changes_text()`, `exposure_record()`), `packs/officeqa/tools/cli.py` + `otables.py` (W0-1..W0-12), `packs/officeqa/observe.py` (frozen parser reads the new printings options), `tests/test_officeqa_pack.py`, `tests/test_officeqa_tables.py` (the S4 tests, pinned to development items), `adapters/*-w0` rebuilt with `arms.py build`, `packs/officeqa/seed_notes.json` deleted (unread). The harness learner prompt lists the W0 changes (no item ids). Base README environment facts (`kit.readme_l0`) and the search-helper example (`campaign.HELPER_README`) carry W0-CONTENT §3. `preflight --replay-v9a` was **not** re-run (needs ~2 min on the v9-a root; open for the integrator). Controls changes of W0-PROPOSAL §(e) (re-noting, hard-block echo, `empty_output_retry`) are **not** applied (DESIGN scope: controls fixed this round; open). |
| 9 | minor | numeric-literal screen bypassable with builtins / string concat | **fixed** — `fold_constant` folds literals, unary/binary arithmetic (through `calc`'s bounded evaluator), string `+`/`*` (≤ 4 KB) and `int/float/str/ord/len/abs/round/chr` on such values; folded strings are number-scanned and length-checked. Post-admission diagnostic **not built** (recorded as a PREREG limitation in PROTOCOL §8). Test: `test_constant_folding_covers_builtins_and_strings`. |
| 10 | minor | code-grant audit tolerates missing fields, no closure recompute | **fixed** — both marker fields required; `code_root_findings` re-derives the grants from the served manifest applied to the code root (`import_closure`) and compares files and listing dirs. Tests: the audit test above (extra granted file → fail; missing `code_sha12` → fail), E2E `test_audit_code_root_and_submit_findings_detect_violations`. |
| 11 | minor | ToolRunner has no memory limit | **fixed differently** — macOS refuses `ulimit -v/-m/-d` and `setrlimit(RLIMIT_AS/RSS/DATA)` (verified: EINVAL / "current limit exceeds maximum"); the runner polls the process group's RSS (`group_rss_kb`, every 0.25 s) and kills it over `memory_mb` (3 GB default; recorded in request.json as `tool_memory_mb`); result carries `memory_capped`. Test: a 400 MB hog with a 128 MB cap is killed within the timeout. |
| 12 | minor | ReDoS in `search_adapter_file` | **fixed** — plain-text search, `\|`-separated alternatives, case-insensitive, first 2,000 chars of a line; description updated. Test: `test_search_adapter_file_is_a_literal_search`. |
| 13 | minor | PREREG "no sockets" claim | **wording written** (PROTOCOL §8, to copy into PREREG-v10.md): tool processes run under the actor's Seatbelt profile with TCP only to the actor's broker port; nested denial deferred. The broker shape check / per-episode cap is **not built** (rejected for this round: Codex's own shell has the same reach, and a body-shape gate would need a Codex request schema the fake upstream tests do not pin). |
| 14 | minor | second ToolRunner construction site | **fixed** — `campaign.make_tool_runner` is the one constructor; `preflight.tool_runner` delegates. Test: orchestration `test_tool_runner_roots_and_construction` asserts identical read roots / env / tmp for the same inputs. |
| 15 | minor | `if tool …; then`, `$(tool …)` not reconstructed | **fixed** — leading `if/while/until/!` stripped; `$(…)` and backticks lifted as their own simple commands. Test: `test_tool_invocations_in_tests_and_command_substitutions`. |
| 16 | minor | USAGE-only revision consumes a replay | **fixed** — the counter and the cap apply only when a replay runs. Test: `test_usage_only_proposal_consumes_no_replay`. |
| 17 | minor | new-tool relevance not stated to the learner | **fixed** — PROMPT_SECTION and REVISION_TOOL_DESCRIPTION say a brand-new tool must be bundled with a change that alters an existing recorded command's output. |
| 18 | minor | Broker `submit_enabled` defaults to "controller present" | **fixed** — default `False`; the campaign remains the only caller turning it on with `arm.gate`; the broker test helper and the live-Codex helper pass it explicitly (keyed on the gate). Test: `test_submit_enabled_defaults_to_false_and_needs_a_controller`. |
| 19 | minor | `[harness:…]` inserts and `.harness/` carry an ARM_REVEALING word | **fixed** — kit directory `.kit/` (`kit.KIT`, README L0, acting PROTECTED, tests, probe script), controller inserts `[runtime:<reason>]`, gate prefix `[runtime:gate]`, the ledger splice head "recorded by the runtime", the submit tool's messages, wrapper/zprofile comments "tool kit"; `evidence.py` still parses the old `[harness:` prefix of v9 records. The E2E leak test greps the bare fragments `harness`, `evol`, `mwm`, `baseline`, `adapters/` over every request input of every arm (scored and verification) — it caught the ledger splice wording on the first run. |
| 20 | minor | no calibration entry point or cost record | **fixed** — `calibrate.py` (`--source-root ../v9-p1-a [--actor-runs 20] [--source-tools w0\|v9] [--dry-run]`): builds a scratch root over the finished root (its scored episodes as one validated evidence block; `prepared` linked; issue-47 references never written), runs `harness_learning.run_learning` with the campaign's ToolRunner factory and a capped verification runner (real episodes through `campaign.episode`, `runs/verify/01/`), writes `preflight/calibration.json` (proposals by kind, screens, replays, tiers, admission, actor runs, cost of the calibration only). `preflight.offline` requires a non-dry record for a harness-learner root (else a pending decision is listed). Tests: `tests/test_v10_calibrate.py` (toy root, scripted model). A $0 dry run against the real `v9-p1-a` root ran end to end (45 released + 5 withheld episodes, status `retained`, 0 actor runs) and left `preflight/calibration.json` marked `dry_run: true` — the real calibration (paid, ≤ 20 actor runs) is for the integrator. |

## File map (all under `officeqa-pro133-v10-plumbing/`)

Changed: `harness_core/runner.py` (ToolRunner: actor-like env, per-run closure grants, `workspace` cwd, memory
watchdog, `import_closure`), `harness_core/ledger.py` (splice head wording), `harness_core/tools/submit.py` (messages),
`harness_core/predicates.py`, `harness_core/gate.py`, `harness_core/contracts.py` (prefix wording), `kit.py` (`KIT='.kit'`, closure delegation, README facts), `acting.py`, `controller_runtime.py`
(`NOTE_PREFIX`), `evidence.py` (prefix regex), `campaign.py` (`Layout.verify_runs`, `VERIFY_DIR`, `tools_sha12`,
`stage_tools_copy`, `make_tool_runner`, `tool_runner` over the copy, verification runner location + neutrality,
`move_partial`, `learn` passes `verify_dir`, HELPER_README), `learner_common.py` (id screen, expect
normalisation, two-pass `replay_jobs`, `expect_coverage`, opaque verification dirs, `default_verify_dir`,
`tool_runner_for` by content, `fold_constant`, `file_words` delta, `PROCESS_CALLS`, literal
`_search_adapter_file` as a common tool, `tools_evidence_section` / `tools_diff_vs_w0` / `w0_changes_text`,
descriptions, request.json fields), `harness_learning.py` (prompt trimmed to the task + (e)), `prompt_learning.py`
(reads tools, prompt wording), `full_audit.py` (grants re-derived, marker fields, `block_adapters`, note text,
verification under `runs/verify`, workspace neutrality for every episode), `cost.py` (verification under
`runs/verify`), `broker.py` (`submit_enabled` default), `preflight.py` (shared factory, calibration record and
check), `packs/officeqa/pack.py`, `packs/officeqa/tools/cli.py`, `packs/officeqa/tools/otables.py`,
`packs/officeqa/observe.py`, `adapters/harness-w0/*`, `adapters/prompt-w0/SEED.md` (rebuilt), `PROTOCOL.md`
(§4, §5, §6, new §8 PREREG statements).

Created: `calibrate.py`, `tests/test_v10_calibrate.py`, `preflight/calibration.json` (dry run), this report.

Deleted: `packs/officeqa/seed_notes.json` (unread since S4).

Tests changed: `tests/test_v10_learners.py` (+9 tests, shared-evidence assertions, opaque dirs),
`tests/test_v10_orchestration.py` (+1 audit test, runs/verify layout, neutral-copy factory, fake learner records),
`tests/test_v10_e2e.py` (+1 parity test, verification-body leak checks, scored_only filter, renamed-root audit
expectation), `tests/test_v10_runner.py` (env keys, grants, memory cap, W0 find_tables output),
`tests/test_v10_broker.py` (helper keyed on the controller explicitly, new default test),
`tests/test_v10_codex_live.py` (keyed on the gate), `tests/test_v10_arms.py` (parity cases on the new SEED),
`tests/test_officeqa_pack.py`, `tests/test_officeqa_tables.py` (S4), the `.harness`→`.kit` / `[harness:`→`[runtime:`
rename across `tests/*.py`, `tests/e2e/*.py`, `tests/probe_kit.sh`.

## Test count

Command: `cd officeqa-pro133-v10-plumbing && V9_SLOW=1 V9_LIVE_CODEX=1 …/.venv/bin/python -B -m pytest -q -p no:cacheprovider tests`

Result (2026-09-29, after every fix): **503 passed in 367.60 s (0:06:07), exit 0** (the baseline before this pass was
469 passed; +34 tests: 9 learner, 2 calibrate, 1 audit, 1 E2E parity, the S4 pack/tables tests and the broker default test
net of renames). Some tests skip without `V9_SLOW`/`V9_LIVE_CODEX`; the run above had both set.

Per file during the pass: `tests/test_v10_learners.py` 64 passed; `tests/test_v10_calibrate.py` 2 passed;
`tests/test_v10_runner.py` + `tests/test_v10_kit.py` 53 passed, 2 skipped; `tests/test_v10_orchestration.py` +
`tests/test_v10_config.py` 58 passed; `tests/test_v10_e2e.py` 22 passed (live Codex, 99 s); `tests/test_officeqa_pack.py` +
`tests/test_v10_arms.py` 89 passed; ledger/codex_live/runner/learners/broker 190 passed.

Two problems surfaced only in the full E2E run and were fixed: the `.harness`→`.kit` rename had also renamed the
weak actor's `view.harness` attribute (the fake upstream then raised on every loop-guard turn and the broker retried
70,000 times: the run hung), and the ledger splice head still said "recorded by the harness" (caught by the new
bare-fragment check).

## Known gaps (open for the integrator)

1. **Paid calibration** (`calibrate.py --source-root ../v9-p1-a --actor-runs 20`, ~$?) has not run; the dry-run record
   in `preflight/calibration.json` is marked `dry_run: true` and does not satisfy `preflight.offline`.
2. `preflight.py --replay-v9a ../v9-p1-a` must be re-run on the S4 tools (expect ≥ 200 rebuilt, 0 changes) before
   launch; PROTOCOL §6 still cites the pre-S4 numbers.
3. W0-PROPOSAL §(e) controls (re-noting every `loop_threshold` repeats, hard-block echo, `empty_output_retry`) are
   not applied: controls are fixed this round by DESIGN; a decision is needed before PREREG.
4. The post-admission "released reference printed verbatim" diagnostic (finding 9) is not built; PROTOCOL §8 records
   the residual exposure for PREREG.
5. The broker does not reject non-Codex `/v1/responses` bodies (finding 13); PREREG wording only.
6. `PREREG-v10.md` is still to be written and hashed (after the calibration); PROTOCOL §8 lists the statements to
   carry, including the exposure record (`packs/officeqa/pack.py: exposure_record()`), the d-vs-e one-factor claim,
   the ToolRunner profile and constants (3 GB memory cap, 80 jobs support-first).
7. The replay cwd is `<tmp>/tool-XXXX/workspace`, not `runs/episodes/NNN/workspace`; a tool inspecting the parent
   of its cwd can still tell the contexts apart (documented; process creation and network remain different by
   design).
8. `preflight/replay-v9a.json` in the tree is the pre-S4 record.
