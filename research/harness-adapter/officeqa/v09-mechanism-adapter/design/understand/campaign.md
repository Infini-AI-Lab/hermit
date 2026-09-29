# v8 campaign orchestration, learning and audits: what v9 plugs into

Area: campaign orchestration, learners, audits, preflight, cost. Source of truth:
`officeqa-pro133-v8-plumbing/` (called `P/` below). The three run roots
`officeqa-pro133-{none,prompt,cards}-v8-work/` hold byte-identical copies of every
`P/*.py`, `PROTOCOL.md` and `cards_domain.md` (checked with `cmp`, 0 differences).
Framework copies: `frameworks-v8/next-a48c0f9` (none, cards) and
`frameworks-v8/prompt-c0c9b87-v6f` (prompt). File:line citations are to `P/` unless a
path is given. Framework short names: `block.py` and `PROMPT.md` =
`frameworks-v8/next-a48c0f9/harnessadapter/evolvers/cards/`; `render.py`, `schema.py` =
`frameworks-v8/next-a48c0f9/harnessadapter/architectures/cards/cards_core/`; `loader.py` =
`frameworks-v8/prompt-c0c9b87-v6f/harnessadapter/adapter/loader.py`; `hosts/codex.py` =
`frameworks-v8/next-a48c0f9/harnessadapter/hosts/codex.py`. No reference answers, graders, answer keys, private-evaluation files
or credentials were opened. Run data was read only for structure, counts, usage and
timing.

## 中文摘要（给用户）

- **循环**：133 题按固定 SHA-256 顺序、每 5 题一个 block（共 27 个，最后一块 3 题）。一题一个 Codex 进程，串行执行。
  每个 block 做完后：先把 5 个答案的哈希写进 seal（封存），再从私有答案文件取出这 5 题的参考答案发给学习者，
  学习者跑完后产生下一版 adapter，然后才开始下一个 block。最后一块之后不学习。全部 133 题封存后才统一评分。
- **学习者**：prompt 组和 cards 组都用同一个 GLM-5.3-flash（Fireworks chat/completions，temperature 0，
  **不带 reasoning_effort**，即 provider 默认值；actor 则是 `high`）。prompt 组学习者会重写整个 ADAPTER.md，
  它能看到本 block 的分数、参考答案、轨迹；cards 组学习者只能「提议卡片」，每张卡要由 actor 重跑（support 题 ×2、对照题 ×2）通过才进牌组，
  它能看到所有已发布 block 的参考答案。
- **v8 里发现的新问题**：prompt 组学习者的原生文件工具（read_file/write_file/validate…）**100% 报错**
  （沙盒内 import harnessadapter 失败），学习者全靠 bash heredoc 改 ADAPTER.md；ADAPTER.md 最后长到 231 KB、92 条 lesson，
  并且整份发给 actor（包括 2 条未验证的 lesson）。
- **v9 要接入的位置**：`experiment.py` 的 profile 名/ARCHITECTURE 推断、`campaign.py` 的学习模块选择、W0 路径、
  workspace 准备，`acting.py` 的只读文件列表和 `--ephemeral`，`data.py` 的 manifest 字典，`preflight.py`、`full_audit.py`、
  `cost.py` 的硬编码假设。一个新 architecture 名如果不改 `experiment.py:12`，会被**悄悄当成 MWM**。
- **成本**（下限，公开价）：none $25.11；prompt $40.06（学习者 $11.82）；cards $49.95（学习者 $4.59 + 验证重跑 $13.03）。

## Glossary (first use)

- **Block**: 5 consecutive questions; the unit after which learning happens (`LEARN_EVERY = 5`, campaign.py:37).
- **Arm**: one experimental condition. v8 code knows only two arm names: `baseline` (no adapter) and `adapted` (carries the profile's architecture) (experiment.py:14-19).
- **W0**: the generic, unlearned starting adapter of an architecture, at `<framework>/harnessadapter/architectures/<ARCHITECTURE>` (campaign.py:34).
- **Seal**: a JSON file of SHA-256 hashes of the submitted predictions, written *before* any reference answer is read, so later tampering is detectable (campaign.py:194-198).
- **Release**: copying the block's official reference answers into the learner's evidence folder after sealing (campaign.py:199-227).
- **Replay** (prompt arm): the controller re-executes the check list written in each lesson (a corpus-line lookup or a grading of a candidate answer) and rewrites the lesson's `status:` line (postmortem.py:280-317).
- **Paired re-run verification** (cards arm): the actor is run again, with the proposed card added, on the questions the card claims to fix ("support") and on questions already right ("keep"/controls) (cards_learning.py:523-578).
- **Prequential hits**: for each admitted card, which later questions its scope matched and whether they were right; recorded before the learner sees the block (cards_learning.py:481-490).
- **Strict / 1%**: the official `reward.py` at tolerance 0 and 0.01.

---

## 1. The block loop

### 1.1 Configuration (per root)

- Each root's `.task-paths.json` sets `framework`, `codex`, `data_cache`, `profile`, `arms` (+ `reference_shelf` hashes for prompt). Read by paths.py:8-18 and experiment.py:6-17.
  - none root: `profile = full_cards_unbounded`, `arms = ['baseline']` (so `ARCHITECTURE='cards'` internally, but no adapter is ever served; campaign.py:336-337 still imports `cards_learning`, unused).
  - prompt root: `full_prompt_only_unbounded`, `['adapted']`, framework `prompt-c0c9b87-v6f`.
  - cards root: `full_cards_unbounded`, `['adapted']`, framework `next-a48c0f9`.
- Profile whitelist experiment.py:7-9; `FULL = PROFILE.startswith('full_')` :10; architecture inferred by substring: experiment.py:12
  `ARCHITECTURE = 'prompt_only' if 'prompt_only' in PROFILE else 'cards' if 'cards' in PROFILE else 'mwm'`; `MWM = ARCHITECTURE == 'mwm'` :13.
- FULL settings: `ACTOR_SECONDS = 3600` :22; `BROKER_OPTIONS` all `None` (no completion/response/timeout/attempt caps) :23-24; `LOCKSTEP = FULL and len(ARMS)==2` :30 (False in v8, single-arm roots); `LEARN_LAST_BLOCK = False` :31; `PROJECT_DOC_MAX_BYTES = 4194304` :32; learner-only reference shelf :34.
- The three roots were launched as three independent processes ~9 s apart (`process.json` started 21:32:29/34/38 UTC). There is no cross-root barrier, so the arms drift apart in time: none finished after 13.7 h, prompt 22.8 h, cards 27.5 h of wall clock (from `state.json` started/finished). Later questions of different arms therefore ran under different provider conditions.

### 1.2 Question order

- `data.SEED = "metaworld-officeqa-pro-codex70-online-v1"`, `COUNT = 133` (data.py:28-31).
- Order = ascending `SHA256(seed + NUL + uid)`, uid tie-break (officeqa_prepare.py:129-136; recorded in the manifest at :200). The first 50 equal the v4-v7 order; preflight checks this against the v7 root's baseline-predictions ids (preflight.py:39-41).
- Tasks are loaded controller-side from `prepared/agent/tasks.jsonl` with hash and order checks (data.py:174-189). Actors receive only the question text through `prompt_for` (campaign.py:108-111).

### 1.3 Loop body (`campaign.run`, campaign.py:331-447)

1. Setup: FULL sets an unbounded global rate scheduler (`max_in_flight=inf`, :333-335); picks the learner module: `cards_learning` if `ARCHITECTURE=='cards'`, else `full_learning` (:336-339); verifies prepared data (:340-342); `freeze()` (:343, defined :292-307) hashes all top-level `ROOT/*.py`, `PROTOCOL.md`, `.task-paths.json`, `cards_domain.md`, the helper tool, the Codex binary, the manifest, W0, the whole `harnessadapter` package, `deployment/` and the reference shelf; writes `state.json` (:344-346, :354-362).
2. `blocks = ceil(133/5) = 27` (:353). For each block (:365):
   - `verify_freeze(frozen)` and `verify_learning_seals()` at block start (:367-368; defined :310-323, :279-284).
   - Actors run **one at a time** (:371-382): `actor(key, arm, ordinal, task, adapter)` (:114-167). Adapter = W0 for `adapted`, `None` for `baseline` (:364).
     - `actor` refuses a pre-existing run dir (:117), prepares the workspace (:120, `prepare_workspace` :77-105 = `acting.prepare_actor_workspace` + copies `officeqa_tools.py` as `officeqa.py` + writes `README.md`), writes `task.json`, opens a `broker.Broker(key, base/'api', local_sandbox=..., **BROKER_OPTIONS)` (:126), runs `acting.run_actor(..., timeout=ACTOR_SECONDS)` (:127-128).
     - Answer = `records/final.md` only if Codex status is `completed` (:133-134); `prediction.json` = `{task_id, answer}` (:135-136).
     - Adapter immutability: revision before == after, else raise (:137-138).
     - Status: `timeout` if wall-clock kill (:142-144), else broker failure kind or Codex status (:145-147). `summary.json` fields :148-166.
   - All-transport-failed block warning (:383-388).
   - Learning, only for `adapted` and not after the last block (:390): `assemble_evidence(block, results)` (:391), then `learning.run_learning(key, adapter, evidence, ROOT/'evolution'/NN, block, **extra)` (:397-398; `extra={'stop': stopping}` only for cards, :397). **The next adapter is `Path(learned['candidate'])` unconditionally** (:399); the learners return the unchanged source on failure (see 2.1). The round is appended to `state['learning']` (:400-403).
   - After the last block: `{'status':'skipped_unused','activated':False}` and no seal/release (:406-411). With 133 questions: 26 learning rounds + 1 skipped.
   - Learning is **synchronous**: the next block's actors wait for it. Learning time on the critical path: prompt 7.47 h of 22.79 h; cards 9.93 h of 27.5 h (includes 8-wide verification re-runs).
3. Errors: any exception in an arm sets `stopping`, aborts the barrier and re-raises (:415-421); `state.status = 'stopped_on_error'` (:442-446). The learners catch their own exceptions (2.1), so only controller-side failures (e.g. `assemble_evidence`, freeze drift) stop a campaign.

### 1.4 Sealing and reference release (`assemble_evidence`, campaign.py:170-276)

- Requires exactly 5 results with ordinals `(b-1)*5+1 .. b*5` (:171-175), and identity of task/prediction/summary vs the frozen task list (:185-190).
- **Seal first**: `private-learning-seals/NN.json` with task and prediction SHA-256 and `sealed_utc`; refuses to overwrite (:194-198).
- **Then** reads `prepared/private/answers.jsonl` for exactly the 5 ids (:199-207).
- Writes `evidence/NN/NNN/` with `task.json`, `prediction.json`, `summary.json` (copied :218-221), `reference_answer.json` = `{task_id, official_answer}` (:222-227), `trajectory.jsonl` = only `agent_message`, `command_execution`, `mcp_tool_call`, `file_change` completed items plus `turn.completed/failed`, passed through `broker.public` (strips reasoning) (:231-244), `failure.json` = termination/accounting fields (:246-251).
- Stamps `reference_released_utc` into the seal (:253-254); `manifest.json` declares `includes_gold: True, includes_grades: False, includes_future_questions: False, includes_baseline_traces: False, includes_source_hints: False` plus the seal hash (:255-258); a README (:259-275).
- **Not** copied: `records/events.jsonl` raw, `api/*` request/response bodies, the actor `workspace/` (so any file the actor writes, e.g. a v9 ledger, is invisible to the learner unless added here).
- Seals are re-verified at every block start and at the end (:368, :426).

### 1.5 Final sealing and grading (campaign.py:426-441, data.py:244-330)

- Per arm, `<arm>-predictions.jsonl` opened with mode `'x'` (:430-433) → `data.seal_predictions` (:434) writes `private-evaluation/predictions-seal.json` (0400) and chmods the prediction files (data.py:269-277).
- `data.grade` (:437) refuses to run unless the seal is complete and unchanged, re-verifies the corpus (data.py:280-302), grades each arm with the official grader at tolerance 0 (data.py:304-307; `officeqa_prepare.grade`), writes `private-evaluation/paired-metrics.json` (data.py:311-329). Grades exist only after all 133 are sealed.
- `run.py` then builds `cost-estimate.json`, `preflight/final-audit.json` (via `full_audit`) and `report.html` (run.py:93-97).
- Separately, **learner-side grading** of each released block happens inside the learner process: `postmortem.grade_block` with `VENDOR/reward.py` at tolerance 0 and 0.01 → `evolution/NN/postmortem/NNN.json` + `verdicts.json` (postmortem.py:147-166). Cards additionally re-grades every released block (cards_learning.py:123-147).

---

## 2. The learner interface

### 2.1 Contract both learners implement

`run_learning(key, source, evidence, output, block[, stop]) -> dict` must: refuse an existing `output` (full_learning.py:193, cards_learning.py:458-459); validate `source` is W0 or this campaign's previous admitted candidate (full_learning.py:23-33 uses `parents[2]/result.json`; cards_learning.py:430-441 uses `parent/result.json`); validate the evidence and seal (`learning.validate_evidence` learning.py:229-263, `validate_batch_seal` :266-307); never raise; always write `output/result.json` with at least `status, activated, candidate, block, model, profile, architecture`, usage fields and `session`; and return `candidate = source` on any failure.

`validate_evidence` enforces: exactly five distinct adapted episodes of this block (learning.py:236-240); `includes_gold is True` and the four `includes_* is False` flags (:241-245); required files `task.json, prediction.json, trajectory.jsonl, reference_answer.json` (:310-326); every other JSON/JSONL row must not contain keys `reasoning_content, api_key, authorization, gold_answer, ground_truth, correct_answer, grader_result, official_answer` (:216-226, applied :256-261). A v9 ledger copied into evidence must avoid those key names.

### 2.2 What each learner receives

| Input | prompt-only learner (`full_learning` + `postmortem`) | cards learner (`cards_learning` + `evolvers/cards/block.py`) |
|---|---|---|
| This block's questions, sealed submissions, references | Yes: `panel()` in the goal (postmortem.py:175-192) and `session/evidence/` (copied by the framework `evolve`, `frameworks-v8/prompt-c0c9b87-v6f/harnessadapter/evolution/session.py:80`) | Yes: `panel()` (cards_learning.py:499) |
| Strict/1% verdicts | This block, in the panel and each phase (postmortem.py:182, :93) | All released blocks (`released_records`, cards_learning.py:123-147; `read_evidence name=reference` returns them, :226-228) |
| References of earlier blocks | No (session evidence is this block only; `verify_answer` refuses other ordinals, postmortem.py:224-226). Earlier lessons carry forward inside ADAPTER.md; replay can check earlier ordinals (full_learning.py:183-188) | Yes: every released ordinal via `read_evidence` and `verify_answer` (cards_learning.py:217-246); pool list shows `Submitted`/`Reference` of every pool question (block.py:263-267) |
| Trajectories | `trajectory.jsonl` of this block (public items only) | Any released ordinal, `trajectory` = compacted (commands cut to 600+1400 chars, outputs to 1500+500, cards_learning.py:157-179) or `trajectory_full` paged at 20,000 chars (:47, :224-233) |
| Corpus | Read paths: corpus, a copy of the index + helper, the block's postmortem dir, the shelf (full_learning.py:202-214) | `search_corpus`, `read_corpus` (≤150 lines), `list_corpus` (cards_learning.py:234-237) |
| Checkers | `verify_cell`, `verify_answer` (this block) answered by the controller (postmortem.py:115-125, :221-235) | `verify_cell`, `verify_answer` (any released), `calc` (AST-whitelisted arithmetic, :188-212) |
| Domain text | `LADDER` S1-S7 + GOAL_COMMON + GOAL_PROMPT_ONLY (postmortem.py:16-86), OfficeQA-specific (S1 vintage rule) | `PROMPT.md` (evolvers/cards/PROMPT.md) + `P/cards_domain.md` (OfficeQA-specific, learner-only) + `status_text` (deck, pool, right, hits, last 12 ledger rows; block.py:249-290) + earlier learner report tail 12,000 chars (cards_learning.py:444-452) |
| Reference shelf | Yes (prompt root only; `excel-365-bible.txt` + indexes, frozen) | No |
| Write channel | Rewrite `ADAPTER.md` (candidate dir); scratch records; `scratch/feedback.json` | Only `propose_card` (screened, max `B_MAX=2` accepted per block, 20 attempts; cards_learning.py:254-278) |
| Structure of the session | One conversation; the controller sends one PHASE per failed episode then a SERVING phase (postmortem.py:93-110, :195-198; full_learning.py:106-110) | One conversation; one nudge if it ends with no proposal (cards_learning.py:367-370) |

Never given to either: master answer key, unreleased references or grades, future questions, baseline-arm trajectories, raw `events.jsonl`/`api` bodies, provider reasoning (manifest `forbidden` list, data.py:134, :162).

### 2.3 prompt-only: from learner output to the next adapter

1. `grade_block` → verdicts (full_learning.py:207); goal = `panel(verdicts, previous_replay)` + `goal_text('prompt_only')` + paths (:210-215); phases = failed episodes + serve phase (:216).
2. Native session prepared by `harnessadapter.evolution.session.evolve(prepare_only=True, definition=EvolverDefinition('basic', prompt_override=PROMPT_ONLY_OPENER))` (:221-230): copies source → `session/adapter` and `session/history`, evidence → `session/evidence`.
3. The GLM conversation runs over an MCP bridge to `full_sandbox.py` (:159-173, :235); controller answers `verify_*` itself (:124-125). Rolling context: at ≥600,000 prompt tokens the learner writes its own checkpoint and restarts from it (:88-98). Every tool call snapshots the candidate before/after for provenance (:117-118, :141-143).
4. Post-session (:240-261): protected-tree checks (source, evidence, framework, protected core, evidence/method/environment/history); native finalize; **replay** rewrites every lesson's `status:` to `verified` iff its check list is non-empty and all checks pass (postmortem.py:280-317); feedback file moved out of the candidate.
5. Admission failures (:262-269): learner not `completed`; protected data changed; native validation failed; provenance/model mismatch; zero successful tools; `adapter.json`/`EVOLVER.md` changed. No semantic gate (`human_semantic_gate: False`).
6. Admitted → `commit_all` and `candidate = session/adapter` (:280-283). Next actor: `hosts/codex.prepare` → `Adapter.context()` returns the whole `ADAPTER.md` (context is a plain path in `adapter.json`; loader.py:154-157) → nested under the common instructions in `AGENTS.md` (acting.py:103-112).

v8 observations (prompt root, from `evolution/NN/result.json` and tool records):
- 26/26 rounds admitted; final ADAPTER.md 230,824 bytes, 92 lessons (90 `verified`, 2 `candidate`). Served `AGENTS.md` grew 1,993 → 115,451 (q066) → 231,899 bytes (q131). **Candidate lessons are served too**; only W0's procedure line tells the actor to ignore them. The docstring claim "served only while checks replay green" (postmortem.py:3-5) is true only in that sense.
- **Every native sandboxed file tool failed in every block**: read_file 0/65, write_file 0/28, list_files 0/28, validate 0/38, read_path 0/25, list_path 0/5 (ok counts). The error is `PermissionError: ... MetaHarnessAdapter/harnessadapter/__init__.py` raised from the controller venv's editable-install finder (`adapter-comparison-work/.venv/.../__editable___harnessadapter_0_1_0_finder.py`) inside the sandboxed helper. Likely cause (inferred, not re-run): full_sandbox.py:12-15 removes `REPOSITORY` from the sandbox's runtime roots, so the helper cannot import the framework from `PYTHONPATH` and falls through to the editable finder, whose target is outside the sandbox. The learner worked around it with `bash`/`run_command` (713 + 1,376 calls, all ok), writing ADAPTER.md through shell heredocs (230 such commands). Admission still passed because the "successful tools" check counts shell calls and validation is host-side (full_learning.py:261, :267). Preflight did not catch this: it checks only that `full_learning` imports (preflight.py:69-70).
- Replay proves citations, not rules: a lesson is `verified` if its cited corpus line contains the cited text and its cited final answer grades strict against a released reference (postmortem.py:290-298). It never re-runs the rule on an episode.

### 2.4 cards: proposals, verification re-runs, deck

1. Grade the block, load all released records, excluded ids (the 18 databricks/officeqa#47 questions, cards_learning.py:53-55, :476), `cards-state.json` and `cards-ledger.jsonl` (:477-478).
2. Prequential hits recorded and saved before learning (:481-490).
3. Pairing: only questions answered under the current deck (`r['block'] >= deck_since`) can be pool (wrong, unclaimed, not transport-lost; block.py:136-139) or right/control (block.py:142-143) (:492-496). Observed pool/right sizes per block: pool 0-12, right 1-9; e.g. block 4 had 1 right question, so only 1 control.
4. Learner session (:281-394): GLM with controller tools only; call cap 400 (:44, :339-341); checkpoint at 600,000 prompt tokens (:342-354); an empty output-cap runaway is re-sent ≤3 times (:357-361).
5. `propose_card` → `block.screen` (block.py:146-168): schema, support ⊂ pool and ≥ `SUPPORT_MIN=1`, leak screen (ids, ordinals, file names, numbers > 400 with ≥3 significant digits, any number equal to a released reference, 8-word copies of a question; block.py:25-128), duplicate of a rejected card; deck byte room (`CARDS_MAX=16384`, render.py:15; cards_learning.py:265-270).
6. Verification (:523-578): each accepted proposal alone is assembled onto the source deck (`verify/Cxxx/adapter`); jobs = each support question × `K=2` + `KEEP=2` controls (scope-matched right questions first, then most recent; block.py:171-177) × `K`; up to `CONCURRENCY=8` actors at once (:43, :576-578); `verify_actor` uses the same `prepare_workspace`, broker options, `run_actor` and 3,600 s wall clock as scored runs (:397-427); a transport-lost run is retried once (`-r1`, :564-568).
7. `block.judge` (block.py:180-212): support passes if misses ≤ (#support − 1) with no inconclusive question; keep passes if no control is wrong in all its runs; cost passes if mean verification output tokens ≤ `COST_MAX=3.0` × the originals. Fewer than K valid runs = inconclusive (neither admitted nor rejected-by-hash).
8. Admitted cards (can be two per block, verified separately, never jointly) are written with `admitted: <block>` via `block.assemble` into `evolution/NN/adapter` and committed (:604-618); a diff check ensures only the new card files were added (:612-616). Durable state (claimed, rejected shas, `next_id`, `deck_since = block+1` on admission, rendered/overflow) and ledger rows are written only at the end (:623-636).
9. Failure: before `state_saved`, the result is `failed`, `candidate = source`; any in-memory ledger rows are appended as `void` (:642-650).
10. Serving: `interface.context()` → `render.compile` = PREAMBLE + admitted cards ordered by (admitted block, −#support, id), cut whole-card at 16,384 bytes (render.py:32-57).

v8 observations (cards root): 63 `propose_card` calls, 62 screened (20 screen-rejected, 42 verified; 1 failed JSON-schema validation for a missing `support`), 21 admitted. Among screen rejections: 6 "deck full" in blocks 21-24 (the 16,384-byte budget was binding from block 21 on), and 12 "scope keyword too short" for phrases like "department of the army", because `schema.validate` rejects a phrase if *any* token is shorter than `KEYWORD_MIN` (`frameworks-v8/next-a48c0f9/harnessadapter/architectures/cards/cards_core/schema.py:127-129`). Totals: 15/26 rounds activated, 11 retained, 0 failed; 240 verification actor runs (0 retries); served `AGENTS.md` 1,603 → 17,282 bytes; `cards-state.json` final: 22 claimed ordinals, 21 rendered, 0 overflow, `deck_since = 27`. Ledger row schemas: `add` rows carry `t, id, card, claim_sha, scope, support, keep, runs[{q, role, k, strict, status, out_tokens, excluded, elapsed_seconds, requests, scheduler_wait_seconds, dir}], tokens_ratio, admitted, inconclusive, reason, rationale, session, utc`; `screen_rejected` rows carry `t, id, card, claim_sha, admitted, reason, rationale, utc`.

---

## 3. Where a new "mechanism" architecture plugs in

### 3.1 Required code changes (each is a hard-coded branch today)

| # | Place | Today | What a `mechanism` arch needs |
|---|---|---|---|
| 1 | experiment.py:7 `PROFILES` | 4 profiles | add e.g. `full_mechanism_unbounded` (and a static variant, see #10) |
| 2 | experiment.py:12 `ARCHITECTURE` | substring test; **anything else becomes `'mwm'`** | add an explicit `'mechanism'` branch. Otherwise `MWM=True` silently turns on the Docker Python sandbox (campaign.py:123-125), the MWM runtime copy (acting.py:98-108, :122-123, :306-309), `EvolverDefinition('mwm')` (full_learning.py:226), MWM lesson parsing (postmortem.py:202, :251), and preflight fails `no_mwm_runtime_in_workspace` |
| 3 | campaign.py:34 `W0` | `<framework>/harnessadapter/architectures/<ARCHITECTURE>` | ship W0 inside a new private framework copy (v8 used `frameworks-v8/...`; do not touch the MetaHarnessAdapter checkout). It is snapshotted by `freeze` and re-checked every block (:300, :317) |
| 4 | campaign.py:336-339 learner import; :397 `extra` | cards vs full_learning | add `elif ARCHITECTURE=='mechanism': import mechanism_learning as learning`; pass `stop` if it launches actors |
| 5 | campaign.py:77-105 `prepare_workspace` | copies `officeqa.py`, writes README | copy the L1 CLI tools (from the adapter, so evolved tool versions are served) and document them; applies to every arm that should get them (L0 fixes must be identical in all arms) |
| 6 | acting.py:153, :215 protected files | `AGENTS.md, README.md, officeqa.py, .codex, marker` | add the tool files so the actor cannot edit them; the actor gets no read access to the adapter dir for a context-only adapter (acting.py:470-471), so tools must be copied in or served by MCP |
| 7 | acting.py:166-174 `tool_environment`, :136-144 / :203-209 read roots | PATH, HOME, TMPDIR only | L0: `TMPPREFIX` (zsh heredocs), `python3` shim, numpy, rg, narrower read roots. These live in acting.py, shared by all arms |
| 8 | acting.py:322-325 `codex exec ... --ephemeral` | one ephemeral turn per question | an L4 commit gate cannot `resume` an ephemeral session; it must live in the broker relay (see #9) or the runner must drop `--ephemeral`. `collect_final` requires `final.md` == last `agent_message` (acting.py:411) |
| 9 | campaign.py:126, cards_learning.py:406, run.py:40 `broker.Broker(...)` | three construction sites | L2/L3/L4 broker options must be passed identically at all three (scored, verification, smoke) |
| 10 | campaign.py:390 learning condition | `arm == 'adapted' and (block < blocks or LEARN_LAST_BLOCK)` | a `mech-static` arm needs a switch that skips release and learning (no such flag exists; only `LEARN_LAST_BLOCK`) |
| 11 | experiment.py:17-19 `ARMS` | subset of `{baseline, adapted}` | four v9 arms = four roots (as v8 did) or a code change; LOCKSTEP only supports two arms (campaign.py:350) |
| 12 | data.py:152-161 manifest | `learning_type[ARCHITECTURE]` dict lookup | add a `'mechanism'` entry, else `KeyError` at `prepare` |
| 13 | campaign.py:218-251 evidence assembly | 6 files per episode | to replay checkers offline, also copy the actor's ledger / full command outputs (the public trajectory is what Codex recorded, not what the model saw) and list them in the manifest; keep `_no_private_fields` key names clear |
| 14 | report.py:22-24 | title per profile | cosmetic |
| 15 | freeze coverage campaign.py:294-296 | only top-level `ROOT/*.py` + 3 named files | any v9 non-`.py` asset in ROOT (checker specs, JSON configs, a `tools/` subdir) is **not frozen** unless added; content inside W0 or the framework copy is |

### 3.2 What `full_audit.py` assumes (run.py:96; the only audit v8 executed)

`audit.py` is a legacy 50-question two-arm auditor (audit.py:24-27) that v8 froze but never ran. `full_audit.build` checks:
- architecture re-derived by the same substring rule (full_audit.py:18) → must be updated together with experiment.py:12, or `generic W0 unchanged` (:22) looks at the wrong directory.
- profile `full_*` and equal to `state.profile` (:19); frozen files, framework, W0, deployment, shelf unchanged (:20-25).
- all configured arms completed 133 in manifest order; each `prediction.json` hash equals `state` (:27-34).
- per question: `invocation.timeout_seconds == ACTOR_SECONDS` (:35-36); Codex retries > 0 (:39-40); `agents_md_bytes ≤ project_doc_max_bytes` (:41-43); `api/summary.json` limits `requests, completion_tokens, per_response, attempts_per_request, request_timeout_seconds` all `None` (:44-47).
- **every** `api/NNN/request.json`: `model == GLM`, `reasoning` equal to what Codex sent (`native_request_settings.reasoning`), and none of `max_output_tokens, max_tokens, max_completion_tokens, thinking, previous_response_id`; `transport.json.upstream == https://api.fireworks.ai/inference/v1/responses`; a request without `transport.json` only if the question timed out (:48-59). → Broker L2/L3/L4 may edit `input` (history) but must not change `reasoning`/caps, and any extra model call must go through the same relay record layout.
- learning rounds: `skipped_unused` has no seal/evidence (:62-65); seal ids = the block's 5 ids, `sealed_utc ≤ reference_released_utc`, prediction and reference hashes match (:66-73).
- if `round['session']`: every `session/control/fireworks-calls/*.json` has `requested_model == GLM`, `request_max_tokens is None`, **`reasoning_effort is None`** (:76-80) — a learner with `reasoning_effort=high` fails this check.
- optional `session/control/postmortem-replay.json`: all `verified` lessons passed (:82-85).
- if `activated`: `session/control/mechanical-admission.json` with `admitted`, `operation_provenance_matches`, `human_semantic_gate is False`, all `protected_unchanged` true (:86-89). (cards writes this file with hard-coded `True` values, cards_learning.py:637-641, so the check is vacuous there.)
- served lesson statuses match replay; `controller_edits.after == revision` (:90-97).

A mechanism learner must therefore write `result.json` (with `session`, `activated`, `candidate`), `session/control/fireworks-calls/NNN.json` in the `client.complete` record shape, and `session/control/mechanical-admission.json`, or `full_audit` must gain a mechanism branch (e.g. check that every admitted checker/tool edit has an offline-replay report).

### 3.3 What `preflight.py` / `run.py` assume

- `checks['profile']` requires kind ∈ `('none','prompt_only','cards')` (preflight.py:33) → add `mechanism`.
- single arm, not lockstep (:34); data verified; 133 tasks; first 50 equal the v7 order, read from the hard-coded v7 root (:24, :39-41).
- `tests/test_v8_broker.py` must pass (:42-45); cards also runs the framework's `tests/test_cards.py` (:46-49). A mechanism branch should add its own offline tests (checkers, tools, replay determinism) here.
- per-kind parity checks against v7/v6f roots (:46-70); `frameworks_are_private_copies` requires the literal string `frameworks-v8` in both paths (:92) → a `frameworks-v9` copy fails until changed.
- offline workspace build and `codex_config_valid` (:77-87); `no_mwm_runtime_in_workspace` (:83); `learner_finds_pinned_codex` (:90); `codex sandbox -P` (:91).
- paid smoke (run.py:15-65): a synthetic note-reading task per arm must finish `completed`, answer exactly the marker string, run ≥1 shell command, no transport failure, and **every** call must carry `reasoning.effort == 'high'` (:47-52). An L4 gate must let a non-numeric marker answer through; L3 nudges must not fire on a 1-command task.
- `run.py run` refuses to start unless `preflight/readiness.json` passed and all source hashes are unchanged (run.py:77-85).
- Missing today: no smoke of the learner's own tools (the prompt learner's broken file tools went unnoticed, 2.3).

### 3.4 Protocol text a v9 evolver must respect or change openly

- manifest `learner_policy.forbidden` includes `"baseline-arm trajectories"` and `"earlier experiments"` (data.py:134, kept in FULL :162). An evolver whose admission replays over v8 episodes, or over the none arm, contradicts the declared protocol unless the manifest and PROTOCOL.md say so. Within-campaign replay over this root's released episodes is consistent with it.
- Released references are the only gold; `verify_answer`-style grading inside a learner uses `VENDOR/reward.py` in the controller (postmortem.py:138-144) and must stay out of actor and learner sandboxes.
- The 18 issue-47 questions are scored but excluded as support/controls in cards (cards_learning.py:53-55); the FINDINGS recommend quarantining lessons from them in v9 as well.

---

## 4. Learner model and configuration (v8)

| | Actors (all arms, and cards verification re-runs) | prompt learner | cards learner |
|---|---|---|---|
| Model | `accounts/fireworks/models/glm-5p3-flash` (acting.py:26) | same (`learning.MODEL`, learning.py:27) | same (`client.MODEL`, client.py:11) |
| API | Codex → local broker → Fireworks **Responses** (`/inference/v1/responses`) | Fireworks **chat/completions** via `client.complete` (client.py:12, :38) passed as `transport` (full_learning.py:235) | same, called directly (cards_learning.py:302) |
| Reasoning | `model_reasoning_effort='high'` (acting.py:28, :270), forwarded unchanged by the broker in v8 (broker.py:91-96) | `unbounded=True` pops `max_tokens, max_completion_tokens, reasoning_effort, thinking` (client.py:43-45) → **provider default**; recorded as `reasoning_effort: None` (full_learning.py:74) | same (cards_learning.py:302, :321) |
| Sampling | temperature 0 (broker.py:90) | temperature 0, `top_k=40`, penalties 0 (client.py:39-40) | same |
| Per-call timeout | none (FULL broker options) | `timeout=None` (full_learning.py:59) | 3,600 s (cards_learning.py:45, :302) |
| Context | Codex window 1,048,576, auto-compact at 800,000 (acting.py:293-295) | self-checkpoint at 600,000 prompt tokens (full_learning.py:89) | same at 600,000 (cards_learning.py:46, :342) |
| Caps | none | none (`budgets` all `None`, full_learning.py:218) | 400 calls (cards_learning.py:44) |
| Provenance check | broker rejects model mismatch | response model must equal MODEL (full_learning.py:81-83, :266) | same (cards_learning.py:327-329, :521-522) |

v8 records confirm: all 2,443 prompt-learner calls and 647 cards-learner calls have `purpose: learner`, `requested_model` = GLM, `reasoning_effort: None`, `response_model` = GLM. So actor and learner differ in reasoning setting; `full_audit.py:78` currently *requires* `None` for learners.

---

## 5. Cost accounting (`cost.py`)

- Prices (public, checked 2026-09-21): uncached input $0.15/M, cached input $0.03/M, output $0.50/M (cost.py:16-20); exact `Decimal` arithmetic (:32-36).
- Unit: every transport attempt of every logical call, counted once, including failed calls (:39-72, :170). Missing usage is "unknown", never zero; the total `estimate_usd` becomes `None` if any attempt is unknown, and `known_cost_lower_bound_usd` is reported instead (:121-135).
- Where it looks (:141-160):
  - `runs/<baseline|adapted>/*/api/NNN/` (`transport.json` attempts) → `baseline_actors` / `adapted_actors` (:143-146)
  - `evolution/NN/sessions/*/control/fireworks-calls/NNN.json` (`meta.attempts`) + an in-flight marker → `learners` (:95-118, :147-150)
  - `evolution/NN/verify/*/q*/api/NNN/` → `verification_actors` (:151-154)
  - `preflight/native-roundtrip-*/*/api/NNN/` → `synthetic_preflight`, excluded from `formal_total` (:157-160)
- Anything a v9 mechanism adds (broker-side GLM checker calls, a second learner process, replay runs that call a model) is **invisible to cost.py** unless written in one of these layouts. Offline deterministic replay costs no tokens but is wall-clock on the critical path if run between blocks.

v8 totals (`cost-estimate.json` of each root; all lower bounds because 5/6/10 attempts had unknown usage):

| Root | Group | Logical calls | Input tokens | Cached | Output | USD (lower bound) |
|---|---|---|---|---|---|---|
| none | baseline actors | 7,041 | 652.7 M | 622.5 M | 3.82 M | 25.11 |
| prompt | adapted actors | 6,354 | 723.0 M | 687.3 M | 4.51 M | 28.23 |
| prompt | learners | 2,443 | 306.2 M | 296.8 M | 3.03 M | 11.82 |
| prompt | **formal total** | 8,797 | | | | **40.06** |
| cards | adapted actors | 8,691 | 843.3 M | 805.5 M | 4.99 M | 32.33 |
| cards | learners | 647 | 81.1 M | 74.7 M | 2.76 M | 4.59 |
| cards | verification actors (240 runs) | 5,913 | 301.1 M | 279.9 M | 2.91 M | 13.03 |
| cards | **formal total** | 15,251 | | | | **49.95** |

Time: actor wall-clock sum 13.7 h (none), 15.3 h (prompt), 17.6 h (cards); learning 0 / 7.5 / 9.9 h; cards verification actors summed 10.7 actor-hours (run 8 at a time). Cached input dominates tokens (~95%), so price is driven by output tokens and uncached input.

---

## 6. Consequences for the v9 design (short)

1. Rename-safe architecture switch: fix experiment.py:12 and full_audit.py:18 together; add data.py, preflight.py, report.py branches (3.1 #1-4, #12, #14; 3.3).
2. L0 belongs in acting.py (environment, read roots, protected files) and is shared by all arms; L1 tools belong in `prepare_workspace` and must be protected and frozen; L2-L4 belong in the broker and must be passed at all three `Broker(...)` sites and keep every `full_audit` request invariant.
3. A commit gate cannot resume an `--ephemeral` Codex run; implement it in the relay or change the runner.
4. The evolver's offline replay needs more evidence than `assemble_evidence` copies today (ledger, untruncated command outputs, what the model actually saw in `api/NNN/request.json`), and must stay within this root's released episodes unless the protocol text changes.
5. Keep the cards infrastructure that worked: seal-then-release, controller-side grading, ledger + `claimed`/`rejected_sha` state, the leak screen, `void` rows on failure, prequential hits; drop what failed: unverified text served whole (prompt: 231 KB, candidate lessons included), tiny control sets caused by `deck_since` pairing, a fixed byte budget that fills up (cards deck full from block 21), and a scope validator that refuses ordinary phrases.
6. Add a learner-tool smoke to preflight (the prompt learner's native file tools failed 100% in v8 unnoticed) and decide explicitly whether learner reasoning stays at provider default (then state it) or matches the actors (then change `client.py` and `full_audit.py:78`).
7. `mech-static` needs a "no learning, no release" switch (campaign.py:390); four arms mean four roots with time drift unless a multi-arm lockstep is added.
8. Cost: extend `cost.py` groups for any new call site; budget the learning phase on the critical path (v8: 33-36% of wall clock for the learning arms).
