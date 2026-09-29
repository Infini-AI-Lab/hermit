# v10 harness adapter: the adapter owns text, tools and controls; the learner evolves text and tools together

Status: draft 2 (rev1, 2026-09-29; draft 1 revised after the three-lens review in `review/DESIGN-REVIEW.md`; every
changed or added line is marked "(rev1)"). Supersedes the v9 factorial design for the next round. Code: a copy of
`officeqa-pro133-v9-plumbing/` named `officeqa-pro133-v10-plumbing/` (v9 roots and code are never modified).

## 中文摘要（写给初学者）

**背景。** v9 预实验发现：GLM 的成绩从 v8 的约 22/50 涨到 31/50，功劳主要来自"环境修好 + 四个表格工具 + 一段建议文字"，
而不是"提交时执行的规则"。规则只在提交那一刻说话，抓不住"读错了题、选错了表"这类错误；模型知道"harness 会检查"之后，
反而少做验证（b2 的 `printings` 调用只有 a 的三分之一）。

**用户的决定（2026-09-29）。** 按框架定义，harness adapter 可以导出三样东西：给模型的文字、工具、子 agent 定义。工具本来就
属于 adapter。所以：
1. **基座保持干净**：Codex CLI + GLM-5.3-flash，只保留让 Codex 在这台机器上正常工作的修复（L0），不带任何外部知识。
2. **adapter 拥有一切外部的东西**：建议文字（context）、工具（tools）、控制（controls）。
3. **进化的重心从"提交时的规则"转到"工具 + 文字一起进化"**：学习器可以改工具代码和用法说明，并且和一条笔记捆绑成一份
   "修订"，用配对重跑验证后才生效。
4. 先用 a/b/b2 三组每题的对比做出更好的起点 adapter（W0），再在同样 50 题上跑五组：plain（干净基座）、prompt-evolve（只有
   文字，只能加小抄）、harness-static（W0）、harness-evolve（W0 + 进化工具和小抄）、harness-evolve-notes（W0 + 只进化小抄）。
   最后一组是审稿人要求加的：它和 harness-evolve 只差"能不能改工具"这一件事，所以两者的差距才能说明"工具进化"本身。(rev1)

**几个词。**
- **adapter（适配器）**：一个目录，里面是 harness 之外的所有东西：`SEED.md`（文字）、`notes/`（按题触发的小抄）、
  `tools/`（工具代码 + 用法说明）、`controls.json`（循环保护等运行时控制）。
- **prompt adapter**：只有文字的 adapter（`SEED.md` + `notes/`）。本轮它的学习器只能添加带触发词的小抄，不能重写 SEED，
  所以在预注册里叫"scoped-note evolution"（范围受限的小抄进化），**不**自称 GEPA/SkillOpt（那两者会整段重写 prompt）。(rev1)
- **harness adapter**：文字 + 工具 + 控制。
- **revision（修订）**：学习器提出的一份改动包：若干工具文件的补丁（patch，精确替换一段旧文本）、用法说明、可选一条笔记。
  整包一起验证、一起生效。(rev1)
- **paired re-run verification（配对重跑验证）**：改动只在"它本该修好的题"上重跑 2 次、在"原本已经答对的对照题"上重跑 2 次，
  修好至少一题且不弄错对照题才采纳（和 v7 cards、v9 notes 一样的机制）。改工具时，对照题从"这次改动真的改变了其输出"的题里选。(rev1)
- **replay（回放）**：零成本检查。把已经发布的题目里模型实际运行过的工具命令，用旧工具和新工具各跑一遍，比较输出。
  新工具让旧命令崩溃就拒绝；哪些题的输出变了，用来挑对照题、判断改动是否和要修的题有关。(rev1)
- **prequential（先答后学）**：每道题都在看到它的参考答案之前作答；每 5 题一个 block，答完才释放参考答案给学习器。所以
  evolve 组在同样 50 题上的成绩是合法的在线学习成绩。

---

## Scope decisions for this round (draft 2a, the owner's cut after the review; binding for the build)

Built now (everything not listed below is built as written): the six arms (whether `harness-evolve-notes` runs is a
launch-time budget decision), `gate` field and gate-off path with gate-off loop templates, neutral code root,
manifest-driven tools from the adapter, calc pinned, controls.json + `checked_controls`, revision definition, patch-based
staging, functional python screen, code-aware diff leak screen with the numeric-literal rule, replay from `raw_cmd`
with tiers and replay-chosen controls (KEEP 2 / 4), relevance screen with `expect`, slots 2 notes + 1 revision,
`ToolRunner`, parser mode + `observation_coverage`, learner evidence for tools (exit codes, `read_tool_output`, friction
digest, taxonomy mapping, diff vs W0), `write_revision` with tools, audits of code root / shas / submit-iff-gate,
calibration round before the paid run, PREREG after S4.

**Deferred or reduced (do not build):**
1. **Grounding judge** (§5): the per-run grounding result is *recorded* in `mechanical-admission.json` when cheap to
   compute from the ledger; it never gates admission this round.
2. **`propose_revert` and prequential revision monitoring** (§5): not built. "Affected by revision R" is computed after
   the campaign by the analysis (replaying later episodes' commands offline) and reported; a later revision may still
   change a file again.
3. **Note-only variant runs of an admitted bundle**: not run (cost). The bundle's components are recorded; attribution
   is left to the postmortem.
4. **Nested `sandbox-exec` profile inside tool wrappers**: not built; the actor's Seatbelt profile already denies
   network and non-workspace writes for every process the actor starts.
5. **Parser mode** is coarse: if any `tools/*.py` sha of the served revision differs from W0, the frozen parser skips
   the re-run for every manifest tool (header recovery + raw lines only). `observation_coverage` is still recorded.
6. **USAGE.md line rule**: each non-empty, non-heading line must contain a manifest tool name or a `--option` string
   that occurs in the candidate's tool sources; nothing more elaborate.
7. **`mechanism_learning.py`** (rule/policy evolution) is removed from the v10 copy with its tests; `rules/` and
   `policy.json` remain valid static controls for a `gate: true` harness adapter (none this round).
8. Cost bound per block: notes ≤ 20 runs + revision ≤ 14 runs = **≤ 34** verification actor runs.

## 0. What v9 taught (evidence for the decisions below)

| finding | evidence |
|---|---|
| Environment repair + tools + 2.5 KB advice text lifted GLM from ~22/50 (v8) to 31/50 (v9 a) | pilot 1, same 50 questions |
| Replacing the text by submit-time rules cost 11 questions (b 20/50); adding the text back recovered most of it (b2 13/25 vs a 15/25 on the same 25) | pilot 1, pilot 2 |
| Rules fired on none of the a/b2 discordant questions except one (candidate already wrong) | pilot 2 traces |
| The model verifies less when a gate exists (b2 `printings` calls 29 vs a 104 on 18 questions) | pilot 2 traces |
| The only offer that ever corrected an answer was `scale`; `competing_candidates` flipped 2 right→wrong | postmortem |
| Loop notes changed no outcome but cut input tokens 322M → 128M (measured with a working `submit`; the gate-off wording of §3 is re-checked on the $0 toy E2E before launch) (rev1) | postmortem |
| T = 1.0 removed all loops but cost 7 questions | pilot 1 |
| Errors are INTERPRETATION / SOURCE_TABLE / CELL_SELECTION / VINTAGE: what the model *saw* decides, not what it is told at submit | divergence analysis (103 runs) + pilot analyses |
| Tool friction is the dominant tool signal in the a traces: empty pages in 30/50 episodes, `OUTPUT CUT` in 41/50, no-match in 20/50, argparse exit-2 on unquoted multi-word `--col` (rev1) | v9-a records, effect review |

Design consequence: put the knowledge where the model looks (tool outputs and the text beside the question), keep the
runtime controls that are free (loop guard), drop the submit gate from the default, and let the learner change the tools.

## 1. Ownership: base harness vs adapter

**Base harness (every arm, identical, contains no task knowledge):**
- Codex CLI 0.155.0-alpha.9.2 + GLM-5.3-flash through the broker relay, temperature 0, no top_p.
- L0 repair: the frozen actor python (Codex's own runtime python with numpy/pandas) and ripgrep on PATH,
  `TMPPREFIX`/`ZDOTDIR` so zsh here-documents work, the Seatbelt read policy, multi-agent off, permissions text off,
  explicit auto-compact 244,800. These fix defects of running Codex in this sandbox; they add no knowledge. (Keeping
  them in the base makes the plain baseline *stronger*, so every adapter claim is conservative.)
- The task environment: COMMON instructions (answer format), README with the corpus path, the published OfficeQA task
  conventions and the FTS5 search helper `officeqa.py`. This is the benchmark's environment, the same for all arms.

**Adapter (everything else):**

| export | files | prompt adapter | harness adapter |
|---|---|---|---|
| context | `SEED.md` (always shown in AGENTS.md), `notes/<id>.json` (scoped, ≤ 3 per question) | yes | yes |
| tools | `tools/manifest.json`, `tools/*.py`, `tools/USAGE.md` (the README section, ≤ 3 KB, every line names a manifest tool or option) (rev1) | no | yes |
| controls | `controls.json` (loop guard, post-compaction splice, submit gate on/off); `rules/`, `policy.json` only when the gate is on | no | yes |
| agents | (sub-agent definitions; multi-agent is off in this round) | no | no |

`adapter.json`: `{"architecture": "prompt" | "harness", "contract": "v10.0", "pack": "officeqa", "revision": "w0" | "<sha12>"}`.
A prompt adapter with `tools/` or `controls.json` is refused; a harness adapter without `tools/manifest.json` and
`controls.json` is refused.

`revision` is defined once (`arms.revision(tree)`) as sha12 of `snapshot(tree minus adapter.json)`, so writing the hash
into `adapter.json` does not change it; W0 keeps the literal `w0`; `check_adapter` asserts `adapter.json.revision` equals
the recomputed value, and the same value goes into `freeze.json`, episode markers and `mechanical-admission.json`. (rev1)

The controller process never imports or executes adapter python: it reads it with `ast`/`py_compile` (`cfile=os.devnull`)
only, and runs it only through the actor sandbox or the `ToolRunner` of §5. A test greps the plumbing for imports from
`adapters/`, `evolution/` or `runs/code/`. (rev1)

## 2. Arms

| arm | adapter | learner | controller |
|---|---|---|---|
| `plain` | none | — | no |
| `prompt-static` | prompt W0 | — | no |
| `prompt-evolve` | prompt W0 | `prompt` (notes) | no |
| `harness-static` | harness W0 | — | yes (controls.json) |
| `harness-evolve` | harness W0 | `harness` (notes + tool revisions) | yes |
| `harness-evolve-notes` | harness W0 | `prompt` (notes only; same W0, tools, controls, SEED, USAGE.md as `harness-evolve`) (rev1) | yes |

`contracts.ArmSpec` keeps its five fields with new values plus a sixth: `seed ∈ {none, text, harness}`, `learner ∈ {None,
text, harness}`, `l1_tools` = tools served from the adapter (harness arms only), `controller` = broker controller (harness
arms only; what it does is in controls.json), `notes` = scoped notes rendered, `gate: bool` (harness arms: from
controls.json; `gate` ⇒ `controller`; every submit-related site keys on `gate`, never on `controller`, see §3). (rev1)
`STANDARD_ARMS` = the six above; the v9 names are retired (`arms.parse_arm` refuses them with a message naming the v10 arms).
`CONTRACT_VERSION 'v10.0'`, config profile `'v10'`; `arms.parse_config` refuses profile `'v9'` with a message naming the v9
tree, so v10 code pointed at a v9 root fails early. (rev1)

Validation: `seed == 'text'` ⇒ `l1_tools == False` and `controller == False` (a prompt adapter has no tools and no
controls); `seed == 'harness'` ⇒ `l1_tools and controller`; `learner == 'text'` ⇒ `seed ∈ {text, harness}` (the
notes-only learner runs on either architecture); `learner == 'harness'` ⇒ `seed == 'harness'`; `seed == 'none'` ⇒ no
notes, no learner; `gate` ⇒ `controller`. (rev1)

## 3. Serving an adapter to an episode

- **Tools.** `kit.build_kit(workspace, arm, tool_wrappers, submit_url, code_roots=[<code root>])` with
  `tool_wrappers` from `tools/manifest.json` via `campaign.adapter_tool_wrappers(adapter)` (replaces the arm-derived
  `campaign.tool_wrappers(arm)`; `prepare_episode` and `Campaign.verification_runner` both take this path, so scored
  and verification episodes always run the served revision's tools, never the pack's). (rev1)
  `{"tools": {"find_tables": {"script": "cli.py", "args": ["find_tables"]}, "show_table": {...}, "printings": {...}, "calc": {"script": "calc.py", "args": []}}}`
  (tool names `[a-z][a-z0-9_-]{0,31}`, not `python|python3|rg|submit`, and not any name that resolves on the actor's
  `BASE_PATH` (`shutil.which(name, path=BASE_PATH)` must be None: a tool named `cat`, `sed`, `grep`, `ls`, `sh`, `env`
  … would shadow the system command for every corpus read the model makes); `script` a regular file under `tools/`;
  `args` plain tokens). (rev1) The code the actor may read = the scripts plus their sibling-import closure inside `tools/` (kit
  `_import_closure`) and a listing of `tools/` — never anything else of the adapter (SEED.md, notes, controls are
  unreadable to the actor; the workspace only gets AGENTS.md and README.md). The kit meta records the adapter revision
  and the sha256 of every granted file; `verify_kit` and `full_audit` check them after the episode.
- **Neutral code root.** The kit wrapper embeds the absolute script path in a readable file, and the actor sandbox
  grants metadata reads on every parent of a code grant, so tools are never served from `adapters/…` or
  `evolution/BB/adapter/…` (those names reveal the architecture and that learning happened). `prepare_episode` copies
  the served revision's `tools/` to `<root>/runs/code/<sha12>/tools/` (read-only, beside the workspace, never inside
  it), records `sha12` in the marker, and passes that directory as the code root. `arms.revealing_fragments` is applied
  to the code-root path, every tool file name and every manifest tool name (`ARM_REVEALING` gains `harness`, `evol`,
  `adapt`, `revision`), `check_adapter` refuses them, and `full_audit.episode` asserts every marker `kit.tools[*].script`
  starts with the recorded code root and every `kit.code.files` sha equals the revision's `tools/` sha. (rev1)
- **Adapter validation before serving.** `arms.check_adapter` (on W0 and on every candidate before verification)
  validates the manifest (names, scripts exist, `calc` present), runs `kit._import_closure` on every script, and does a
  dry-run `kit.build_kit` into a scratch workspace, so a broken revision fails at staging and never aborts a scored
  episode. (rev1)
- **calc** moves into the adapter (`tools/calc.py`, a copy of `harness_core/tools/calc.py` at W0). In this round it is
  hash-pinned to the W0 copy: `stage_tool_patch`/`stage_tool_file` refuse `tools/calc.py`, `check_adapter` asserts its
  sha equals W0's, and the replay skips `calc` commands (its output cannot change). Reason: its `RUNNER` string uses
  `exec`/`eval`, which the §5 AST screen bans with no exemptions, and no analysis item proposes a calc change; a later
  round may rewrite `RUNNER` without `exec`/`eval` and unpin it. The ledger still parses its frame
  (`⟦calc sha=…⟧ … ⟦result⟧ … ⟦end⟧`) from the output. (rev1)
- **README.md** = L0 + helper + task conventions + (harness) `# Tools\n\n` + `tools/USAGE.md` + (gate on)
  `SUBMIT_README`. **AGENTS.md** = COMMON + `SEED.md` + chosen notes (unchanged from v9).
- **Controls** (`controls.json`, harness adapters):
  `{"loop_guard": true, "loop_threshold": 3, "loop_notes_max": 3, "splice": true, "ledger_splice_bytes": 8000, "gate": false, "enforce_requery_max": 0}`.
  Validated by a new `contracts.checked_controls()` with `CONTROLS_DEFAULTS`/`CONTROLS_RANGES` (separate from
  `POLICY_*`, which stay for the gate-on rules). (rev1)
  With `gate: false` the kit adds no `submit` wrapper, the README has no submit section, `/v1/submit` answers 404,
  `on_final` never re-queries, and `rules/`/`policy.json` are refused. With `gate: true` the v9 mechanism runs as
  before (rules, policy).
  The gate-off path is threaded explicitly through every v9 site that keyed on `arm.controller`: `kit.build_kit(...,
  submit=arm.gate)`, `kit.tool_environment`/`acting.actor_env_extra` require `HARNESS_SUBMIT_URL` iff `arm.gate`,
  `campaign.compose_readme` appends `SUBMIT_README` iff `arm.gate`, `Broker(..., submit_enabled=arm.gate)` (404
  otherwise), `campaign.build_controller` passes a `NullGate` whose `check_final`/`submit` raise if ever called,
  `full_audit` asserts "submit section iff gate", `run.smoke` requires `submits >= 1` iff gate, `report.TITLES` and
  `campaign.LEARNER_MODULES` name the v10 arms. One test per site builds a `harness-static` episode with `gate: false`
  and asserts: no `bin/submit`, no `HARNESS_SUBMIT_URL`, no submit README section, `POST /v1/submit` → 404, no
  enforce rows. (rev1)
  Loop guard and splice keep their v9 code (`controller_runtime.MechController`), reading their knobs from controls.json
  instead of policy.json; `loop_guard: false` makes `_loop_guard` return `[]`. (rev1)
  **Gate-off loop templates.** The v9 templates tell the model to run `submit`, which does not exist with the gate
  off, and escalate on `Candidate` events that only `submit` creates. With `gate: false` the controller uses
  `LOOP_OPTIONS_NOGATE` ("Options: end your turn with the value you trust, or run a command you have not run yet."),
  drops the "Current candidates" line, and escalates on the newest `Compute` event after the last new evidence ("Your
  last calc after new evidence printed {value}; answer now.", falling back to `last_answer_line`). After
  `loop_notes_max` notes the guard applies the postmortem R6 hard block: an identical command is refused with
  "blocked: identical output already shown". The selected templates are counted in `arms.harness_fixed_text` and
  `learner_common.controller_template_bytes` (a test pins their equality), and CodexSim tests cover a loop under
  `gate: false`. (rev1)
- **Ledger.** The broker derives command / observe / compute / candidate / note / compaction events as in v9. The
  pack's `observations` parser is frozen (it parses the W0 tools' options) and re-runs a parsed invocation with the
  pack's W0 tools to derive cells; it never imports the served revision (§1). Parser mode per invocation: when the
  served revision's sha for the invoked tool's script (and its import closure) equals W0's, re-run as in v9; otherwise
  skip the re-run and use only the self-describing first-line recovery and raw-line observations, so the ledger never
  records a W0 value under a locator the evolved tool printed differently. Every Observe records the parser mode and
  the served revision; a re-derived line that is not present verbatim in the output the model saw falls back to a
  raw-line observation. Per episode the ledger counts commands parsed by invocation / recovered by header / raw only
  and writes `observation_coverage` to `summary.json`; the learner status and `report.py` show the block average.
  `observations` never raises; a test with a modified `show_table` format asserts no exception and no observation
  whose text is absent from the seen output. (rev1)
  Ledger observations feed only the splice and the learner's evidence in this round (no rules).
- **Model-visible text budget** (both architectures): `SEED.md` + all notes rendered + `tools/USAGE.md` (harness) +
  the controller's fixed loop templates (harness, gate-off variants) ≤ 16,384 B. `tools/USAGE.md` ≤ 3,072 B and
  every line must name a manifest tool or an option that the manifest/tool diff actually adds or removes (regex over the
  manifest and the argparse option strings), so USAGE.md cannot become an always-visible prompt channel. Tool code:
  ≤ 12 files, ≤ 196,608 B per file, ≤ 393,216 B total, python only (W0 is 123 KB, `otables.py` alone 111 KB). (rev1)
- **Prompt adapter, no tools.** Its SEED.md must not name the four tools (they do not exist in that workspace):
  `arms.check_w0` refuses a prompt W0 whose SEED.md contains `find_tables`, `show_table`, `printings` or `calc`; the
  same check runs on every note proposed in a `prompt-evolve` arm. (rev1)

## 4. W0 adapters (seed parity in knowledge, not in wording)

- `adapters/harness-w0/`: `SEED.md` = the v10 advice text (from `officeqa-v10-analysis/synthesis/W0-PROPOSAL.md`,
  ≤ 4 KB), `notes/.keep`, `tools/` = the v10 tools (`cli.py`, `otables.py`, `calc.py`, `manifest.json`, `USAGE.md`;
  the W0 tool changes decided by the analysis are made in `packs/officeqa/tools/` and copied, so the frozen
  observation parser matches W0), `controls.json` as in §3.
- `adapters/prompt-w0/`: `SEED.md` = the same knowledge written for a workspace without the tools (search with
  `officeqa.py`/`rg`, read the table lines with `sed -n`, check later issues for revisions, FY vs CY, repaired rows,
  choose the table by title, compute with python, answer format), `notes/.keep`.
- `arms.py build` regenerates both from `packs/officeqa/seed_text_harness()` / `seed_text_prompt()` and the pack
  tools; `check_w0` pins the committed copies (drift check) — as in v9.
- **Parity checklist.** `packs/officeqa/SEED_KNOWLEDGE` enumerates the knowledge items (latest printing unless a date
  is pinned; FY vs CY; repaired / identity-fail rows; choose the table by title and headers; compute with code; answer
  count and percent form; do not repeat commands; …), each with one sentence for the harness SEED and one for the
  prompt SEED. `check_w0` asserts every item's sentence is present in the respective SEED; both SEEDs are ≤ 4 KB and
  within ±25 % of each other in bytes (or the difference is declared in PREREG); PREREG records both SEEDs' hashes,
  byte counts and the total model-visible bytes per arm (harness: SEED + USAGE.md + templates; prompt: SEED). (rev1)
- **Exposure record.** `synthesis/W0-PROPOSAL.md` must exist and list, per W0 tool change, the development items it was
  designed from (e.g. lineage by table+panel+row: UID0007/UID0133/UID0220; raw SUMMARY values: UID0180;
  period-labelled long rows: UID0027/UID0028/UID0117; `find_tables` ranking/caps: UID0180/UID0182/UID0211;
  identity-fail cross-check: UID0007), noting which are issue-47 disputed items. PREREG copies this list and every
  contrast is reported on all 50 and excluding the targeted items (§7). (rev1)
- W0 directory names are never served to the actor (the code root of §3 is the neutral copy). (rev1)

## 5. Learning: revisions verified by paired re-runs

Both learners run through `learner_common.run_round` (same model GLM-5.3-flash, reasoning effort high, budget,
evidence, controller-owned tools `list_episodes / read_episode / search_corpus / read_corpus / verify_cell /
verify_answer / calc`, note format, note verification, note retirement). They differ in the proposals they may make:

| | `prompt` learner (`prompt_learning.py`, = v9 text learner) | `harness` learner (`harness_learning.py`) |
|---|---|---|
| `propose_note(id, triggers, text, support, rationale)` | yes | yes |
| `read_adapter_file(path, start_line=1, num_lines=200)` (paged, numbered lines) (rev1) | SEED.md, notes | + tools/*.py, tools/USAGE.md, tools/manifest.json, controls.json |
| `search_adapter_file(path, pattern)` (rev1) | — | line numbers + text of matches in a tools file |
| `read_tool_output(ordinal, step)` (rev1) | yes | yes: the full recorded output of a manifest-tool command (evidence pages are cut at 1500+500 chars) |
| `stage_tool_patch(path, old, new)` (rev1) | — | exact, unique substring replacement in one candidate `tools/` file (Edit-tool semantics: refused when `old` is absent or not unique); ≤ 20 patches per proposal |
| `stage_tool_file(path, content)` | — | a **new** file only (`tools/<name>.py` ≤ 16 KB, or `tools/USAGE.md`, `tools/manifest.json`); existing python files are changed by patches (rev1) |
| `try_tool(argv, stdin=null)` | — | run a tool from the candidate `tools/` (staged edits applied) through the `ToolRunner` over the real corpus/index; returns exit code + ≤ 8 KB stdout + stderr tail; ≤ 20 s; ≤ 30 calls per session (rev1) |
| `propose_revision(id, support, rationale, expect, note=null)` | — | bundle the staged files (+ USAGE.md) and an optional note into one proposal; `expect` = `{ordinal: [step, …]}` names, per support episode, the recorded command(s) whose output the change should alter (rev1) |
| `propose_revert(revision_id, rationale)` (rev1) | — | propose restoring the tools of an earlier revision; verified by replay + controls only (no support requirement) |

`SEED.md` and `controls.json` are fixed in all evolve arms in this round (learned text = scoped notes only, as in v9).
The one-factor contrast for "the learner may also change tools" is `harness-evolve` vs `harness-evolve-notes` (same
W0, tools, controls, SEED, USAGE.md, evidence; only the learner's tool proposals differ). `prompt-evolve` vs
`harness-evolve` is a *system* contrast (adapter architecture, W0, learner evidence and learner all differ) and is
labelled so in PREREG. Because the `prompt` learner can only append ≤ 3 triggered notes of ≤ 600 B and never rewrite
the seed, PREREG calls that arm "scoped-note evolution (paired-rerun admitted)", not GEPA/SkillOpt (those rewrite the
whole prompt with candidate-pool selection); giving both learners a screened `stage_text_file('SEED.md')` is deferred to
a later round. A revision may bundle a note, so a tool change and the sentence that tells the model when to use it are
verified together; the bundle's components (tool files, USAGE.md, note) are recorded, and for an admitted bundle the
note-only variant is also run on the support questions (K = 2, reported, not gating) so the report can say how many
admitted tool changes were necessary. (rev1)

**Learner evidence for tools** (controller-side, $0): the `harness` learner's prompt carries (a) every manifest-tool
COMMAND line with its exit code, and outputs readable in full through `read_tool_output`; (b) a per-block friction
digest keyed by episode and grade: per tool, invocations, exit ≥ 2 with the first stderr line, empty pages
(`rows 0-0 of 0`), `OUTPUT CUT`, no-match, identical command repeats; (c) the failure-stage taxonomy with the tool/text
mapping (SOURCE_TABLE → `find_tables` ranking/captions/listing; CELL_SELECTION, EXTRACTION → `show_table` labels/NOTES/
repairs and `printings` keys and verbatim values; OPERATIONAL → error messages and option aliases; INTERPRETATION,
FORMULA, answer form → notes); (d) `tools/USAGE.md`, `manifest.json`, the diff of the current revision vs W0, and the
list of W0 tool changes already made from the analysis (so they are not re-proposed); (e) guidance that additive output
changes and option aliases are the edits most likely to pass and generalise, while a change that alters existing
values needs a control that exercised it. (rev1)

**Screens on a staged file (zero model cost, immediate error text back to the learner):** (rev1)
1. path matches `^tools/[a-z][a-z0-9_]{0,31}\.py$ | ^tools/USAGE\.md$ | ^tools/manifest\.json$`, resolves under the
   candidate `tools/` dir, module name not in `sys.stdlib_module_names` (cli.py puts `tools/` first on `sys.path`);
   `tools/calc.py` refused (pinned, §3); ≤ 12 python files, ≤ 196,608 B per file, ≤ 393,216 B total; new files ≤ 16 KB;
   manifest names per §3 (kit `_TOOL_NAME`, not reserved, not on `BASE_PATH`, no revealing fragment). (rev1)
2. python, a *functional* screen (the Seatbelt profile — writes only in the workspace, network only to the broker port,
   `ACTOR_SECONDS` — is the security guarantee; an evolved tool adds no capability the actor lacks): `py_compile` with
   `cfile=os.devnull`; every top-level import resolves to a sibling `.py` in `tools/` or to a module importable by the
   frozen actor python (probed once, cached); an AST walk refuses `Call` nodes whose func is `exec`, `eval`,
   `__import__` or `compile` (no exemptions; calc.py is pinned); `socket, ssl, http, urllib, ftplib, smtplib, xmlrpc,
   asyncio, multiprocessing, ctypes, importlib, pickle, shelve, marshal` are refused as "useless here" feedback, not as
   security. The string-level `open(...,'w')` check is dropped. PREREG states that a tool process may not open
   sockets; where feasible the tool wrapper denies network through a nested `sandbox-exec` profile. (rev1)
3. **code-aware diff leak screen** (the v9 `pack.leak_screen` run on whole tool files rejects W0 itself: file globs,
   issue stamps and page budgets look like leaks). Findings count only when new relative to the source revision's file
   (delta rule; W0 passes by construction). (a) The text screen (`leak_screen`, 8-gram question copy, `UID\d{4}`,
   `q\d{3}`, `block \d`, `episode`, `ARM_REVEALING`) runs over string literals, comments and docstrings of changed lines
   (extracted with `tokenize`), over identifiers, file names, manifest names, USAGE.md and the bundled note; the "large
   number" and "corpus file name" rules apply to USAGE.md and the note only. (b) Numeric literals: refuse any new
   literal, or constant-foldable expression (`4600*1000`), whose value equals a released reference exactly or agrees
   with one to ≥ 3 significant digits and is not a year; refuse new dict/list literals of numbers, new string literals
   > 64 chars and base64-looking tokens; report (not refuse) other new constants ≥ 3 significant digits. (c) Tests: every
   W0 tool file restaged unchanged passes with zero findings; a planted file that prints a released reference as split
   arithmetic and as a dict keyed on question words fails. The screen's version hash is recorded in PREREG. (rev1)
4. manifest: valid names, scripts exist, `calc` present and pinned.
5. text budget with USAGE.md counted (§3 caps).
6. **replay** — runs inside `propose_revision` (not at staging), at most 4 per session, each counted in
   `PROPOSAL_ATTEMPTS`. Jobs are structured records `{tool, argv, stdin, episode, step, grade}` rebuilt from
   `Command.raw_cmd` by `observe.segments()` and a manifest-driven `tool_invocation()` (accepts any manifest name or
   `cli.py <name>`; loop bodies and pipeline segments after the tool are dropped; `Command.tool` is the Codex call name
   and is never used as the filter); de-duplicated on argv; up to 80 most recent; `calc` skipped. Each job runs with
   the source tools and the candidate tools through the `ToolRunner` (4 workers, 20 s CPU/wall each, 300 s wall cap
   per replay; over cap → "replay incomplete", refused). Precondition: ≥ 5 jobs exit 0 with the source tools, else the
   replay is "unavailable" and the revision is refused (a runner regression can never look like a clean replay); 0
   replayable jobs is a failure unless the revision touches only USAGE.md. Reject when a job that exited 0 with the
   source now exits ≥ 2, times out or crashes, or when the candidate's `show_table`/`printings` first line no longer
   matches `HEADER_RX`/`PRINTINGS_RX` (the ledger contract is part of the harness). **Relevance:** reject when no
   command named in `expect` produces a changed line (a change that alters no support episode's output cannot have
   fixed it), and when a new option/tool is referenced by neither the bundled note nor USAGE.md. Output: a per-job table
   (exit before/after, lines added/changed/removed, episode grade) written to `mechanical-admission.json` with wall time
   and counts (cost group `replay_seconds`), used by the control chooser and the tier below. (rev1)
7. **tier** from the replay table: `additive` (only added lines, exit codes unchanged) or `value-changing` (any changed
   or removed line on a right episode); recorded in the ledger and shown to the learner. (rev1)

**ToolRunner** (rev1): `SandboxRunner.run_tool(tools_dir, argv, *, stdin: bytes, env, cpu_s=20, timeout_s=20,
output_cap=16 KB)` execs `<frozen python> -I -B <tools_dir>/<script> <manifest args> <argv>` with `OFFICEQA_CORPUS` /
`TABLES_INDEX` passed through `env -i K=V …`, stdin from a pipe, in a profile that reads exactly the candidate (or
source) `tools/` dir, `prepared/agent/corpus` and `prepared/agent/tables` (index opened read-only/immutable), with
`tmp_root = evolution/BB/tmp`. It is constructed in one place (`campaign.tool_runner(block)`), which asserts that no read
root is inside, equal to, or a parent of `evidence/`, `private-evaluation/`, `prepared/private/`, `runs/` or
`evolution/` (other than the candidate tools dir and tmp); a test covers the assertion. Preflight runs every W0 manifest
tool once through it, and replays the v9-a evidence against unchanged W0: ≥ 200 invocations rebuilt, 0 changes.

**Admission = paired actor re-runs** (`learner_common.verify_revisions`, a generalisation of `verify_notes`): the
candidate adapter = source + revision (+ note), validated by `check_adapter` (dry-run kit) before any actor run;
support questions (1–3, from the pool the learner names; each must be a released wrong episode; `SUPPORT_MAX = 3` in
contracts, was 5 in the v9 code) × K = 2, plus KEEP matched control questions the source answered right × K = 2. (rev1)
**Controls for a revision** come from the replay table, not from note triggers (a tool change is global): first the
right released episodes whose recorded commands changed output under the candidate (changed/removed lines first),
then right episodes with the most invocations of the touched tool; `additive` tier → KEEP = 2; `value-changing` tier →
KEEP = 4, and a replay-changed control that misses even one of its K runs rejects. A replayed command from a right
episode whose output fed a ledger `Compute` input (observe links) and now prints different numbers is refused at screen
time unless the learner's rationale names that episode as intentionally changed (recorded). (rev1)
Judge = cards `block.judge` (one support question right in every run, each further support allows one miss, no control
lost); for revisions the output-token ratio is recorded but not gating this round (COST_MAX false-rejected 3/15 passing
notes in v7 and a tool that prints more is biased upward). **Grounding:** a support run counts as right only if every
number feeding its final `calc` (Compute inputs) is matched by an Observe event whose text the frozen parser finds
verbatim in the cited corpus file (external data such as CPI exempt as in v9); a value printed by an evolved tool that
is absent from the corpus is not grounded; the per-run result is recorded in `mechanical-admission.json`. (rev1)
Slots per block: ≤ 2 notes + ≤ 1 revision (a revision bundling a note uses the revision slot), so the harness learner
cannot spend every slot on notes and behave as the `prompt` learner. (rev1)
The new revision is written by `write_revision(source, target, *, tools: {relpath: bytes}, usage, manifest, add_notes,
remove_notes)` with expected added/changed entries per file (provenance check as in v9), `adapter.json.revision` per §1,
and the tree made read-only; `mechanical-admission.json` records screens, replay table, tier, controls, grounding and
per-run out_tokens; `learning-ledger.jsonl` records every proposal (kind: note / revision / bundle / revert) with its
verdict and every screen rejection reason, so a zero-admission round is attributable to the screen or to the model. (rev1)
Notes keep v9 retirement. **Tool revisions are monitored prequentially:** for each later episode its commands are
replayed against the pre- and post-revision tools to flag "affected by revision R"; the round status and the report
show accuracy on affected vs unaffected episodes per revision; a revision whose affected-episode accuracy falls below the
pre-admission rate by the note retirement rule is flagged, and the learner may `propose_revert` (a later revision may
also change a file again). (rev1)

Cost bound per block (from the constants, asserted by a test): notes ≤ 2 × (3 support + 2 controls) × 2 = 20 runs;
revision ≤ 1 × (3 support + 4 controls) × 2 = 14 runs, plus the note-only variant of an admitted bundle ≤ 3 × 2 = 6; total
≤ 40 verification actor runs (≈ $10 at pilot-1 rates); replay and try_tool are $0. (rev1)

**Calibration before the paid run** (rev1): one `harness` learner session on the v9-a evidence (the 50 released
episodes as one block, v9 tools as the source revision) with ≤ 20 actor runs: does it stage the SUMMARY `%g` fix, the
caption lookback or a `--grep` hint; do screens, replay and paired re-runs admit it on UID0180-style supports? PREREG
records the outcome and the expected number of tool proposals/admissions per block (the analysis-derived fixes are
already in W0 — 4 of 13 per-question analyses name a TOOL fix — so few discoverable targets remain, and the calibration
is what separates "the learner cannot do it" from "the pipeline never let it").

## 6. Fairness invariants (what makes the comparison meaningful)

1. Same actor, temperature, environment, task README (minus the tools section), question order and blocks in every arm.
2. Same learner model, effort, budget, evidence, proposal cap, verification protocol in both evolve arms; the
   `harness-evolve-notes` arm additionally shares W0, tools, controls and evidence with `harness-evolve`. (rev1)
3. Seed parity in knowledge: prompt W0 and harness W0 carry the same conventions (checklist, §4); only the harness W0
   can act on them through tools and controls. (rev1)
4. Nothing model-visible names the arm: root names `v10-r1-{a,b,c,d,e}`; `ARM_REVEALING` extended with `harness`,
   `evol`, `adapt`, `revision`; the check covers the served code-root path, tool file names, manifest names, USAGE.md
   and learner-written notes, not only the root name. (rev1)
5. The learner never sees an unreleased reference; issue-47 references are withheld from both learners (v9 rule).
6. Every served adapter revision is hashed (§1 definition); the audit re-derives the kit's code grants from the revision
   and compares every `tools/*` and `controls.json` sha of a served revision with the admitting round's
   `mechanical-admission.json` and the episode markers; a `controls.json` that differs from W0 this round is refused. (rev1)
7. The controller process never imports or executes adapter python (§1); replay and `try_tool` read roots exclude every
   private tree by construction (§5 ToolRunner). (rev1)

## 7. This round's run (development set, first-50 subset, descriptive)

Roots `v10-r1-a` plain, `v10-r1-b` prompt-evolve, `v10-r1-c` harness-static, `v10-r1-d` harness-evolve, `v10-r1-e`
harness-evolve-notes; T = 0; subset `pilot-first50` (sha c95f9613…a07d); blocks of 5; barrier `v10-r1-barrier`. (rev1)
Named contrasts and their exposure (rev1):
- `d` vs `e`: one factor — the learner may also change tools (exposure-matched, same W0);
- `d` vs `c` and `e` vs `c`: evolution helps (exposure-matched);
- `b` vs `a`: notes-only evolution on the prompt architecture (exposure-matched on the text side);
- `c` vs `a`, `b` vs `d`: system contrasts, optimistic for the harness side by construction — the W0 tool changes were
  designed from named development items (§4), and the prompt W0 carries no tools, so every tool-side dev fix lands only
  on the harness side. The draft-1 sentence "harness-evolve vs prompt-evolve share the same exposure" was wrong and is
  withdrawn.
Reported: strict correct per arm, per block, on all 50 and excluding the items each W0 tool change targeted; paired
discordant counts per named contrast; per-block prequential curves; discordance tables against a (31/50), b, b2 (25
questions only), v8 none, Opus, labelled as cross-day comparisons carrying provider drift; learner rounds (proposals by
kind, screen rejections by reason, replay stats, verification runs by role with per-run strict and out_tokens, admitted
revisions with diff size, files, tier; harness-evolve vs harness-static discordance split by replay-affected /
unaffected episodes and by revision; manifest-tool invocation and friction counts per arm); cost. (rev1)
**Status: descriptive.** One repeat at T = 0 cannot separate the expected effect (0–1.5 questions per 50 in the v9
effect review) from same-configuration flips (~27 % of items, SD ≈ 3.6 questions on a difference of counts): no
hypothesis test; a paired difference within ±4 questions is reported as noise, and only ≥ 8 is described as more than
noise; go/no-go rules for the next step are pre-declared as in v9 P2. Repeats of `c`/`d`/`e` are the first spend of any
later budget. (rev1)
`PREREG-v10.md` (hashed only after S4, when the W0 content exists; `run.py` refuses a root whose `freeze.json` prereg
hash differs; post-hash changes go in a dated addendum) pins: the roots and neutral names; the subset sha with the 5
issue-47 ids in it (UID0007, UID0154, UID0155, UID0213, UID0237: scored, withheld from all learners, so learners see ≤ 45
references); order_seed, repeat, T = 0, no top_p, `ACTOR_SECONDS`; both W0 revision hashes, per-file tool hashes, SEED
hashes and byte counts, model-visible byte totals per arm and the parity checklist; controls.json values and
`CONTROLS_RANGES`; learner constants (model, effort, budget, K, KEEP by tier, SUPPORT_MAX = 3, slots, PROPOSAL_ATTEMPTS,
replay/try_tool caps); screen version hashes (leak, code-aware diff screen, import list, reserved names) and replay
thresholds; grader and Codex binary hashes; the primary metric (strict `reward.py`, sealed `final.md`, no fallback) on
all 50 and the 45 undisputed; the exposure statement per contrast with the targeted dev items; the calibration outcome
and expected proposals/admissions per block; the descriptive status, noise band and go/no-go rules; what is reported per
block and per episode; the b2 = 25 questions caveat; analysis script hashes. (rev1)
Confirmation on unseen questions (non-Pro OfficeQA or SpreadsheetBench) comes after this round and is required before
any claim about tool evolution. (rev1)

## 8. Build plan (v10 plumbing)

- S1 core: contracts v10 (`CONTRACT_VERSION 'v10.0'`, profile `v10`, ArmSpec values + `gate`, STANDARD_ARMS (six),
  `checked_controls` schema + ranges, `SUPPORT_MAX = 3`, tool caps, revision definition), arms.py (W0 builders/checkers
  for both architectures, `SEED_KNOWLEDGE` parity check, `check_adapter` for tools/controls with manifest validation,
  import closure and dry-run kit, `model_visible` with USAGE.md, `revealing_fragments` over paths/names), kit (neutral
  code root, manifest wrappers, calc pinned, submit iff gate, `BASE_PATH` name check), campaign/acting/notes_render
  (`adapter_tool_wrappers`, README composition, controller from controls.json, `tool_runner`), controller_runtime
  (knobs from controls; gate-off templates and escalation; hard block), broker (`submit_enabled`), ledger/observe
  (parser mode, coverage). (rev1)
- S1b rename: delete `tests/test_v9_pilot*.py` and `run.py`'s pilot builders from the copy (the v9 tree keeps the pilot
  record), port the remaining tests to `test_v10_*`, replace preflight's seed-parity drift check with the checklist and
  byte counts plus a test that `prompt-w0/SEED.md` names none of the manifest tools; one gate-off test per site (§3). (rev1)
- S2 learners: `prompt_learning.py` (renamed text learner), `harness_learning.py` (read/search/patch/stage/try/propose/
  revert tools, friction digest, taxonomy prompt), and in `learner_common`: `verify_revisions` with replay-based control
  choice and tiers, grounding check, `write_revision` with tools, the functional python screen, the code-aware diff leak
  screen, replay (`observe.segments` + manifest-driven `tool_invocation`, ToolRunner), candidate tools dir, slots,
  prequential revision monitoring. (rev1)
- S3 audits and protocol: preflight (W0 idempotence through every screen, ToolRunner smoke, v9-a replay ≥ 200 / 0
  changes), full_audit (code root, revision shas, submit iff gate), cost (`replay_seconds`), report (new arms, tier and
  affected/unaffected tables), run for the new layout; toy pack W0 tools (`lookup.py`, `calc.py`) and the $0 toy E2E
  including a loop under `gate: false`; `PROTOCOL.md` update. (rev1)
- S4 W0 content: `synthesis/W0-PROPOSAL.md` with the per-change exposure list, the v10 SEED texts (both, with the
  parity checklist), the W0 tool changes from the analysis (pack tools updated, adapters rebuilt). (rev1)
- S5 protocol freeze: calibration round on the v9-a evidence; `PREREG-v10.md` written and hashed after S4 with the
  contents of §7. (rev1)
- Reviews (validity, engineering/security of tool evolution, effect), fixes, full test run, $0 E2E, paid readiness
  smoke, launch.
