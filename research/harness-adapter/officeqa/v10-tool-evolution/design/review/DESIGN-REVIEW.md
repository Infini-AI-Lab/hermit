# v10 design review: 42 findings from three lenses, deduplicated and applied (rev1, 2026-09-29)

Input: `DESIGN.md` draft 1 and 42 findings (validity 14, engineering 14, effect 14). Every claim about the v9 code
was re-checked against `officeqa-pro133-v9-plumbing/` before acceptance (sizes, constants, regexes, runner limits).
No answer key, grader, `evidence/` or `private-evaluation/` folder was opened; no answer value appears here.
Output: this file plus `../DESIGN.md` draft 2, where every changed or added line carries "(rev1)".

## 1. 中文摘要（写给初学者）

- 三位审稿人（validity 有效性、engineering 工程、effect 效果）共提了 42 条意见；去重后归成 **14 条必须改**（含 5 条会让
  实验完全失效的 blocker）、**6 条小改**、**7 条不采纳或改用别的做法**。
- 最严重的问题是：draft 1 的"工具文件筛查"和"沙箱回放"用在 W0 自己的工具上就会**全部失败**——`otables.py` 有 111 KB，超过
  64 KB 上限；泄漏筛查会把语料文件名、期号当成泄漏；`calc.py` 里本来就有 `exec`/`eval`；沙箱跑不起工具（环境变量为空、
  CPU 只有 2 秒）；回放按 Codex 的调用名过滤，一条命令也匹配不上。结果是"工具进化"组一个工具修订也提不出来，实验白跑。
- 第二严重的是**对比设计**：prompt-evolve 和 harness-evolve 之间差了五件事（有没有工具、有没有控制、SEED 文字、学习器证据、
  能不能改工具），不是"只差能不能改工具"。修法是加第五组 `harness-evolve-notes`（同样的 W0 工具，学习器只能加小抄）。
- 第三是**泄露组别**：工具脚本的绝对路径会写进模型可读的启动脚本，路径里有 `adapters/harness-w0`、`evolution/03` 这种字样。
  修法是把工具复制到中性的、按哈希命名的目录再提供给模型。
- 其他必改项：关闭提交闸门后循环提示仍叫模型运行不存在的 `submit`；配对重跑时工具修订的对照题要按"回放显示输出变了的题"选；
  工具代码可以把参考答案硬编码进去，要用"逐行 diff 的代码筛查 + 答案必须能在语料里找到（grounding）"堵住；修订哈希自指；
  学习器看不到工具报错；预注册文件必须在 W0 内容做完之后再哈希，并声明本轮是描述性的（噪声带 ±4 题）。
- 改工具的方式从"整文件重写"改为"补丁（精确替换一段旧文本）"，`calc.py` 本轮钉死在 W0 不允许改。
- 修改后的 DESIGN.md 结构不变，共 72 处标记 "(rev1)"。

## 2. Required changes (blockers and majors), deduplicated

Format: **N. title** — merged findings (lens#: validity V, engineering E, effect F, numbered in input order) — section —
old → new (the new text is what now stands in DESIGN.md; long passages are abbreviated to their operative sentences).

### Blockers

**1. The static screens and caps reject the W0 tool files themselves** — V2, E2, F1, F2 (whole-file staging).
Verified: `otables.py` 111,481 B > 64 KB; `leak_screen` on W0 files returns 'issue file stamp: 1999_12', 'corpus file
name: treasury_bulletin_*.txt', 'large number: 3,524 …' (otables), 'large number: 728' (cli), 'large number: 103774 …'
(calc); `calc.py:47-49` contains `exec(compile(` and `eval(compile(`.
- §3 text budget. Old: "Tool code: ≤ 12 files, ≤ 262,144 B total, python only." → New: "Tool code: ≤ 12 files,
  ≤ 196,608 B per file, ≤ 393,216 B total, python only (W0 is 123 KB, `otables.py` alone 111 KB)."
- §5 screen 1. Old: "≤ 64 KB per file" → New: path regex `^tools/[a-z][a-z0-9_]{0,31}\.py$ | ^tools/USAGE\.md$ |
  ^tools/manifest\.json$`, resolves under candidate `tools/`, not in `sys.stdlib_module_names`, `tools/calc.py` refused
  (pinned), caps as above, new files ≤ 16 KB.
- §5 screen 2. Old: "no `exec`/`eval`/`__import__`/`open(...,'w'...)` calls (string-level check …)" → New: AST walk
  refusing `Call` nodes whose func is `exec`/`eval`/`__import__`/`compile`, no exemptions; `open()` string check
  dropped.
- §5 screen 3. Old: "leak screen (`pack.leak_screen`) and the 8-word question-copy check over the file text, USAGE.md
  and the note" → New: "code-aware diff leak screen … Findings count only when new relative to the source revision's
  file (delta rule; W0 passes by construction) … text screen over string literals, comments and docstrings of changed
  lines … 'large number' and 'corpus file name' rules apply to USAGE.md and the note only … Tests: every W0 tool file
  restaged unchanged passes with zero findings."
- §5 learner tools. Old: `stage_tool_file(path, content)` "stage one file of the candidate `tools/`" → New:
  `stage_tool_patch(path, old, new)` (exact unique replacement, ≤ 20 per proposal) for existing files;
  `stage_tool_file` only for new files ≤ 16 KB / USAGE.md / manifest; `read_adapter_file(path, start_line, num_lines)`
  paged; `search_adapter_file(path, pattern)`.
- §3 calc. Old: "so the learner can change it too" → New: "In this round it is hash-pinned to the W0 copy … the replay
  skips `calc` commands … Reason: its `RUNNER` string uses `exec`/`eval` … no analysis item proposes a calc change."
- §5 ledger record. New: every screen rejection reason is written to `learning-ledger.jsonl`.

**2. The sandbox runner cannot execute any tool; replay and try_tool are vacuous** — V11, E1, E8, F7.
Verified: `runner.py:114-115` `stdin=DEVNULL, env={}`; RLIMIT_CPU default 2 s; `otables.default_corpus()` raises when
`OFFICEQA_CORPUS` is unset.
- §5 try_tool row. Old: "in the sandbox runner over the real corpus/index" → New: "through the `ToolRunner` … ≤ 30 calls
  per session".
- §5 new paragraph **ToolRunner**: `SandboxRunner.run_tool(tools_dir, argv, *, stdin, env, cpu_s=20, timeout_s=20,
  output_cap=16 KB)` execs `<frozen python> -I -B <tools_dir>/<script> <manifest args> <argv>` with
  `OFFICEQA_CORPUS`/`TABLES_INDEX` via `env -i K=V`, stdin from a pipe, read roots exactly candidate/source `tools/`,
  `prepared/agent/corpus`, `prepared/agent/tables`, `tmp_root = evolution/BB/tmp`; built only by
  `campaign.tool_runner(block)`, which asserts no read root touches `evidence/`, `private-evaluation/`,
  `prepared/private/`, `runs/`, `evolution/`; preflight runs every W0 tool once and replays v9-a evidence (≥ 200
  invocations, 0 changes).
- §5 replay precondition. New: "≥ 5 jobs exit 0 with the source tools, else the replay is 'unavailable' and the
  revision is refused."
- Calc: no fork needed because calc is pinned and skipped (change 1).

**3. Replay selects on `Command.tool`, the Codex call name, so it replays nothing** — F3 (+ E1 structured jobs).
Verified: `ledger.py:513` `tool=call_tool_name(item)`.
- §5 screen 5 (now 6). Old: "ledger `command` events whose tool is one of the manifest tools; up to 80, most recent
  first … re-run … in the sandbox runner (20 s each). Reject when a command that exited 0 with the source now exits
  ≥ 2 … report the number of commands whose printed numbers changed (information for the learner and the log)" →
  New: "Jobs are structured records `{tool, argv, stdin, episode, step, grade}` rebuilt from `Command.raw_cmd` by
  `observe.segments()` and a manifest-driven `tool_invocation()` … `Command.tool` … is never used as the filter …
  0 replayable jobs is a failure unless the revision touches only USAGE.md … per-job table (exit before/after, lines
  added/changed/removed, episode grade) … used by the control chooser and the tier."

**4. The b-vs-d contrast is not one-factor; the two learners' text powers differ; the exposure caveat is wrong** —
V1, V6, V7, V8, E12(USAGE), F(prompt naming).
- §2 arms table. New row: "`harness-evolve-notes` | harness W0 | `prompt` (notes only; same W0, tools, controls, SEED,
  USAGE.md as `harness-evolve`) | yes". Old: "`STANDARD_ARMS` = the five above" → "the six above". Old validation
  "`learner == 'text'` ⇒ `seed == 'text'`" → "`learner == 'text'` ⇒ `seed ∈ {text, harness}`".
- §5. Old: "this keeps 'notes-only text evolution' vs 'notes + tools evolution' a one-factor contrast" → New: "The
  one-factor contrast … is `harness-evolve` vs `harness-evolve-notes` … `prompt-evolve` vs `harness-evolve` is a
  *system* contrast … PREREG calls that arm 'scoped-note evolution (paired-rerun admitted)', not GEPA/SkillOpt …
  `stage_text_file('SEED.md')` is deferred to a later round."
- 中文摘要 item 4 and the "prompt adapter" gloss: "对标 GEPA/SkillOpt" → "在预注册里叫 'scoped-note evolution' … 不自称
  GEPA/SkillOpt"; five arms listed.
- §3 text budget / §1 table. New: "`tools/USAGE.md` ≤ 3,072 B and every line must name a manifest tool or an option
  that the manifest/tool diff actually adds or removes".
- §4 new bullets **Parity checklist** (`packs/officeqa/SEED_KNOWLEDGE`, `check_w0` asserts each item's sentence,
  SEEDs ≤ 4 KB and within ±25 %) and **Exposure record** (per W0 tool change, the dev items it came from; noting
  issue-47 items; `synthesis/W0-PROPOSAL.md` must exist — it is empty today).
- §7. Old: "harness-evolve vs harness-static and harness-evolve vs prompt-evolve share the same exposure and are not
  affected" → New: named contrasts with exposure: "`d` vs `e`: one factor … `c` vs `a`, `b` vs `d`: system contrasts,
  optimistic for the harness side by construction … The draft-1 sentence … was wrong and is withdrawn." Reports on all
  50 and excluding targeted items.
- §3 prompt adapter. New: the "must not name the four tools" check also runs on every `prompt-evolve` note.

**5. The kit wrapper path and code-root parent grants reveal the arm and the learning state** — V3, E7.
Verified: `kit.py:133-139` embeds the absolute script path; `arms.py:77-85` checks the root name only.
- §3 new bullet **Neutral code root**: tools copied to `<root>/runs/code/<sha12>/tools/` (read-only, beside the
  workspace), sha12 in the marker, passed as the code root; `revealing_fragments` over code-root path, tool file names,
  manifest names; `ARM_REVEALING` gains `harness`, `evol`, `adapt`, `revision`; `full_audit.episode` asserts script
  paths and file shas.
- §6 invariant 4. Old: "root names `v10-r1-{a,b,c,d}`; `ARM_REVEALING` extended with `harness`" → New: "`{a,b,c,d,e}`
  … the check covers the served code-root path, tool file names, manifest names, USAGE.md and learner-written notes".
- §4 new line: W0 directory names are never served to the actor.

### Majors

**6. `gate: false` has no off switch (nine sites key on `arm.controller`) and the loop guard names a missing `submit`** —
V4, E3, E4.
Verified: `controller_runtime.py:311-312` templates; `kit.py:264-265`, `campaign.py:207-208`, `broker.py:969`.
- §2. New: sixth ArmSpec field `gate: bool`, `gate ⇒ controller`.
- §3 controls. New paragraphs: `contracts.checked_controls()` with `CONTROLS_DEFAULTS`/`CONTROLS_RANGES`; the nine
  sites listed with `submit=arm.gate`, `submit_enabled=arm.gate`, `NullGate`, `full_audit` "submit iff gate",
  `run.smoke`; one test per site; `loop_guard: false` ⇒ `_loop_guard` returns `[]`; **Gate-off loop templates**
  (`LOOP_OPTIONS_NOGATE`, no candidates line, escalation on the newest `Compute` event with `last_answer_line`
  fallback, R6 hard block after `loop_notes_max`, templates counted in `harness_fixed_text` and
  `controller_template_bytes`, CodexSim loop test under `gate: false`).
- §0 loop-notes row: "(measured with a working `submit`; the gate-off wording of §3 is re-checked on the $0 toy E2E)".

**7. Episode preparation and the verification runner still serve the pack's tools** — E5.
Verified: `campaign.py:213-224` `tool_wrappers(arm)` returns pack paths; `prepare_episode` passes no `code_roots`.
- §3 Tools. Old: "`kit.build_kit(workspace, arm, tool_wrappers, submit_url, code_roots=[<adapter>/tools])` with
  `tool_wrappers` from `tools/manifest.json`" → New: "… `code_roots=[<code root>]` with `tool_wrappers` from
  `tools/manifest.json` via `campaign.adapter_tool_wrappers(adapter)` (replaces the arm-derived
  `campaign.tool_wrappers(arm)`; `prepare_episode` and `Campaign.verification_runner` both take this path …)".
- §3 new bullet **Adapter validation before serving**: `check_adapter` validates the manifest, runs `_import_closure`,
  dry-run `build_kit` into a scratch workspace.

**8. Controls for a tool revision are unmatched to the change; global regressions pass; tools are never retired** —
V5, F4, F10 (tiers), E(SUPPORT_MAX).
Verified: `learner_common.py:65` `SUPPORT_MAX = 5`; `choose_keep` picks by triggers or recency.
- §5 admission. Old: "support questions (1–3 …) × K = 2, plus KEEP = 2 matched control questions the source answered
  right × K = 2" → New: "`SUPPORT_MAX = 3` in contracts, was 5 in the v9 code … **Controls for a revision** come from
  the replay table, not from note triggers … `additive` tier → KEEP = 2; `value-changing` tier → KEEP = 4, and a
  replay-changed control that misses even one of its K runs rejects. A replayed command from a right episode whose
  output fed a ledger `Compute` input and now prints different numbers is refused at screen time unless the rationale
  names that episode."
- §5 new screen 7 **tier** (`additive` / `value-changing`).
- §5 retirement. Old: "tool revisions are not retired automatically (a later revision may revert a file)" → New:
  "**Tool revisions are monitored prequentially** … 'affected by revision R' … accuracy on affected vs unaffected
  episodes per revision … the learner may `propose_revert`"; new learner tool `propose_revert(revision_id, rationale)`.
- Cost bound. Old: "≤ 2 proposals × (3 support + 2 controls) × 2 = 20" → New: "notes ≤ 20 runs; revision ≤ (3 + 4) × 2
  = 14, plus note-only variant ≤ 6; total ≤ 40 (≈ $10)", asserted by a test from the constants.

**9. A tool revision can memorise released references and pass paired verification** — E6, V2(b).
- §5 screen 3 (b). New: "refuse any new literal, or constant-foldable expression, whose value equals a released
  reference exactly or agrees with one to ≥ 3 significant digits and is not a year; refuse new dict/list literals of
  numbers, new string literals > 64 chars and base64-looking tokens; report other new constants"; planted-file test.
- §5 judge. New **Grounding**: "a support run counts as right only if every number feeding its final `calc` is matched
  by an Observe event whose text the frozen parser finds verbatim in the cited corpus file … a value printed by an
  evolved tool that is absent from the corpus is not grounded".
- Retirement/revert per change 8.

**10. The import whitelist is incoherent as security; tool names can shadow system commands** — V9, E9, E14.
- §5 screen 2. Old: "AST import whitelist = stdlib minus … (the Seatbelt profile is the real guard …)" → New: "a
  *functional* screen (the Seatbelt profile … is the security guarantee …): `py_compile` with `cfile=os.devnull`; every
  top-level import resolves to a sibling `.py` … or to a module importable by the frozen actor python … network
  modules are refused as 'useless here' feedback, not as security … PREREG states that a tool process may not open
  sockets; where feasible the tool wrapper denies network through a nested `sandbox-exec` profile."
- §3 manifest names. New: "not any name that resolves on the actor's `BASE_PATH` (`shutil.which(name, path=BASE_PATH)`
  must be None …)".
- §1 new paragraph: "The controller process never imports or executes adapter python … A test greps the plumbing";
  §6 new invariant 7.

**11. Replay and try_tool have no resource bound on the controller host** — E10, V11.
- §5 screen 6. New: "runs inside `propose_revision` (not at staging), at most 4 per session, each counted in
  `PROPOSAL_ATTEMPTS` … 4 workers, 20 s CPU/wall each, 300 s wall cap per replay; over cap → 'replay incomplete',
  refused … wall time and counts (cost group `replay_seconds`)"; try_tool ≤ 30 calls per session.

**12. Revision hashing is self-referential; controls.json and tools/ escape the audit** — E11.
- §1 new paragraph: "`revision` is defined once (`arms.revision(tree)`) as sha12 of `snapshot(tree minus adapter.json)`
  … W0 keeps the literal `w0` … the same value goes into `freeze.json`, episode markers and
  `mechanical-admission.json`."
- §5. Old: "`write_revision` (tools/ replaced by the candidate tools, notes added, `adapter.json.revision` = sha12 of
  the tree)" → New: "`write_revision(source, target, *, tools, usage, manifest, add_notes, remove_notes)` with expected
  added/changed entries per file … the tree made read-only".
- §6 invariant 6. New: compare every `tools/*` and `controls.json` sha with `mechanical-admission.json` and markers;
  a `controls.json` that differs from W0 this round is refused.

**13. Renaming the arms breaks the frozen pilot record, preflight, run.py and seven test files** — E12.
- §2. New: "`CONTRACT_VERSION 'v10.0'`, config profile `'v10'`; `arms.parse_config` refuses profile `'v9'`".
- §8. New item S1b rename: delete `tests/test_v9_pilot*.py` and `run.py` pilot builders from the copy, port tests to
  `test_v10_*`, replace preflight's seed-parity drift check, one gate-off test per site.

**14. The learner cannot see tool output, exit codes or friction; no relevance check; W0 pre-consumes the targets** —
F5, F6, F7(calibration), V12 (bundle attribution), E13 (coverage), V13/F8 (parser after revision).
- §5 new paragraph **Learner evidence for tools** (a)–(e): exit codes on COMMAND lines, `read_tool_output`, friction
  digest, taxonomy → tool/text mapping, USAGE/manifest/diff vs W0 and the W0 change list, tier guidance.
- §5 `propose_revision` signature. Old: `propose_revision(id, support, rationale, note=null)` → New:
  `propose_revision(id, support, rationale, expect, note=null)`; screen 6 **Relevance**: "reject when no command named
  in `expect` produces a changed line … and when a new option/tool is referenced by neither the bundled note nor
  USAGE.md".
- §5 slots. Old: "At most B_MAX = 2 proposals (notes and revisions together) per block" → New: "≤ 2 notes + ≤ 1
  revision (a revision bundling a note uses the revision slot)"; bundle components recorded; note-only variant of an
  admitted bundle run on supports (K = 2, reported).
- §5 new paragraph **Calibration before the paid run** on the v9-a evidence; expected proposals/admissions per block in
  PREREG; §0 new evidence row on friction.
- §3 Ledger. Old: "an evolved tool with new options degrades to the self-describing-first-line recovery or to raw-line
  observations … degradation is harmless" → New: parser mode per invocation (re-run only when the invoked tool's sha
  equals W0's; otherwise header/raw only), parser mode and revision recorded per Observe, verbatim-in-seen-output
  fallback, `observation_coverage` in `summary.json`, format-break test; replay rejects a `HEADER_RX`/`PRINTINGS_RX`
  break.

**15. PREREG contents, hashing order, descriptive status and noise band** — V7, V10, V14.
- §7. New: PREREG "hashed only after S4 … `run.py` refuses a root whose `freeze.json` prereg hash differs … dated
  addendum" with the full pin list (roots, subset + issue-47 ids, seeds, hashes, byte counts, controls, learner
  constants, screen versions, grader/Codex hashes, primary metric on 50 and 45, exposure per contrast, calibration,
  noise band, go/no-go, per-block/per-episode report items, b2 = 25 caveat, script hashes). **Status: descriptive** with
  the ±4 / ≥ 8 rule and "Repeats of `c`/`d`/`e` are the first spend of any later budget"; cross-day discordance tables
  labelled as carrying provider drift. §8: S3 loses PREREG; new S5 "protocol freeze" after S4.

## 3. Minors (applied where cheap, otherwise noted for S2/S3)

1. Judge cost rule for revisions (F11): applied — the output-token ratio is recorded, not gating, for revisions this round.
2. Metrics to record for the postmortem (F12): applied in §7 "Reported" and §5 ledger records (proposals by kind, screen
   rejections by reason, replay stats, runs by role, tier, affected/unaffected discordance, friction counts).
3. Observation coverage (E13): applied in §3 Ledger (`observation_coverage`, block average in status and report).
4. stage path regex and stdlib shadowing (E14): applied in §5 screen 1.
5. Read roots enumerated (E8): applied in the ToolRunner paragraph with the constructor assertion and test.
6. Tier pre-registration (F10) and bundle attribution (V12): applied (screen 7; note-only variant reported); the expected
   additive/value-changing split goes into PREREG at S5.

## 4. Rejected or replaced, with reasons

1. **Give both learners `stage_text_file('SEED.md')` (V6 option A).** Deferred, not adopted: it widens both learners'
   power in the same round that introduces tool evolution, needs a whole-prompt screen and admission path that does not
   exist, and the one-factor contrast is already clean once arm `e` exists. Adopted option B (SEED fixed, USAGE.md
   diff-limited, honest naming in PREREG).
2. **Spend the fifth root on a repeat of c/d instead of another arm (V10).** Rejected: without arm `e` no statement about
   tool evolution per se is possible at all; repeats are declared the first spend of any later budget and the run is
   descriptive.
3. **Load the served revision's tools into the observation parser by path (F8).** Rejected: it contradicts the invariant
   (E9, now §1/§6-7) that the controller never executes adapter python, and an evolved tool loaded into the broker could
   raise or hang inside the ledger. Replaced by parser modes (skip re-run when the tool's sha differs from W0, raw-line
   fallback, coverage counter) from V13/E13.
4. **Add `subprocess`, `os.system`, `pty`, `signal`, … to the AST ban as a security measure (V9).** Rejected as security
   (Seatbelt is the guard; the string/AST list is bypassable and would have banned the pinned calc.py); kept only what
   is a fairness or functional issue: `BASE_PATH` name shadowing, stdlib module shadowing, network modules as
   feedback, and the "no sockets" statement plus optional nested sandbox profile.
5. **Split `otables.py` into modules at W0 (F2, optional).** Rejected for this round: it changes the module the frozen
   observation parser imports and risks silent behaviour drift in W0; `stage_tool_patch` plus the diff-based screens
   make single-file editing workable.
6. **Refuse literals that agree with a released reference to ≥ 2 significant digits (V2 b).** Replaced by ≥ 3 significant
   digits or exact match: two digits would refuse ordinary constants such as `1.2` whenever a reference starts with 12.
7. **Run calc's `RUNNER` in-process or grant process-fork in the ToolRunner (E1).** Moot: calc.py is pinned and skipped
   by the replay this round, so the runner needs no fork grant; unpinning calc (with a `RUNNER` rewrite that avoids
   `exec`/`eval`) is listed as a later-round change.
8. **8 replay workers (F3) vs 4 (E10).** 4 adopted: the controller host also runs five actor roots.

## 5. Contradictions resolved (for the record)

| topic | positions | resolution |
|---|---|---|
| per-file cap | 192 KB/384 KB (E2) vs no per-file cap, 256 KB total (F2) | 192 KB/384 KB plus patch-based staging |
| calc.py | rewrite RUNNER (E2) vs freeze this round (F1) | pinned to W0 this round; AST screen has no exemptions |
| exec/eval screen | AST check (E2) vs drop string checks (E9) | AST check on `exec/eval/__import__/compile` kept; `open()` check dropped |
| parser after revision | skip re-run (V13) vs import served tools (F8) | skip re-run + coverage; controller never executes adapter code |
| fifth root | arm `e` (V1) vs repeat c/d (V10) | arm `e`; descriptive run; repeats next |
| grounding vs parser skipping | E6 grounding needs verified observations; V13 skips re-runs for evolved tools | grounding = number found verbatim in the cited corpus file by the frozen parser (no tool re-run needed) |
| proposal slots | B_MAX = 2 shared (draft) vs reserved slots (F6) | 2 notes + 1 revision; cost bound ≤ 40 runs |
