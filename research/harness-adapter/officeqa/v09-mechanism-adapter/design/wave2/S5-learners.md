# S5-learners: the two learners and their shared infrastructure (wave 2)

**Status: built and tested.**

| check | result |
|---|---|
| New learner tests | 38 passed, 1 slow test skipped by default (5 s) |
| Slow tier | 1 passed (10 s): the model view of all 133 recorded v8 none-arm runs |
| Whole suite, including S6's new orchestration tests | 312 passed, 48 opt-in skips (84 s) |

Rules kept:
- No paid call. No network except 127.0.0.1 (none was needed).
- No credential was read.
- No reference answer or grader of the real benchmark was opened. The only real data read were the v8
  none-arm `api/` records (the model's own traffic) and `cards_learning.py` / `block.py` source.
- This report contains no answer values.

Paths are relative to `officeqa-pro133-v9-plumbing/`.
`PY=/Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv/bin/python`.

## Terms used below

- **Learner.** A second model session that runs after each block of 5 questions. It reads the finished
  episodes and their released answer keys, then proposes changes to the *adapter* (the knowledge and
  controls the actor gets on later questions).
- **Episode.** One actor run on one question.
- **Released.** An episode whose reference answer has been handed to the learner. That happens only after
  the episode's answer was sealed.
- **Evidence.** What the learner may look at: the released episodes of this campaign.
- **Model view / trajectory.** The sequence of messages, commands and tool outputs *as the actor model saw
  them*. It is rebuilt from the broker's `api/NNN/request.json` files, which hold exactly what was sent to
  the model.
- **Compaction.** When the actor's context gets too long, Codex asks the model to summarise and continues
  from the summary. The model never sees the pre-compaction text again.
- **Paired re-run verification.** The cards machinery from v8:
  1. Re-run the actor with the proposed note on the questions it claims to fix (*support*), twice each.
  2. Re-run it on 2 questions it already got right (*controls*), twice each.
  3. Admit the note only if support flips and no control is lost.
- **Counterfactual.** Replaying a recorded episode offline and asking "had this rule existed, would it
  have fired, and would its suggested alternative answer have been right?" It costs no model call.
- **Wilson upper bound.** A conservative upper limit for a rate estimated from few examples. For example,
  0 false fires on 10 right episodes still has an upper bound of 0.28. With 0 fires, the bound drops under
  0.20 only at 16 episodes.
- **Prequential.** Each question is scored once and then used for learning. A note's later results are
  therefore honest out-of-sample evidence.
- **Leak screen.** A check that refuses adapter text containing answer values, question ids, file names or
  copied question wording.

## 1. What was built

| File | Lines | Role |
|---|---|---|
| `evidence.py` | 705 | The one evidence builder both learners use. It holds the block validation (seals, private fields), the model-view trajectory, the controller and ledger views, and the `Grader` wrapper (controller-side grading). |
| `learner_common.py` | 1,367 | The shared learning round, which `run_round` runs. See the list below. |
| `text_learning.py` | 47 | `run_learning` for `text-evolve`. It adds only a prompt section; the only edit tool is `propose_note`. |
| `mechanism_learning.py` | 505 | `run_learning` for `mech-evolve`. It adds `propose_rule`, `modify_rule` and `set_policy`, their zero-cost admission, the policy replay, and the rule vocabulary and status section. |
| `tests/fake_learner_model.py` | 92 | `ScriptedModel`: a fake learner model with the `client.complete` return shape. It can script tool calls, text, output-cap runaways and transport failures, and every message carries a private reasoning string so tests can prove it never reaches a file. |
| `tests/test_v9_learners.py` | 990 | 39 tests (§3). |

What `learner_common.py` contains:
- the session loop;
- the controller-owned tools;
- the note format, note selection and note screen;
- the text budget;
- the leak and 8-word checks;
- the cards `judge`, run concurrently against an injected `actor_runner`;
- retirement;
- state, ledger and source lineage;
- adapter revision writing with a diff check;
- `result.json` and `mechanical-admission.json`;
- `FireworksModel`, the production model client.

### 1.1 Evidence (`evidence.build_evidence(root, block, grader=, exclude=)`)

**Scope.**
- Every released block 1..b is validated, like `learning.validate_evidence`, but with the seal location
  taken from the root:
  - the manifest flags (`includes_gold` true; grades, future questions, baseline traces and hints false);
  - the seal file's sha;
  - the task, prediction and reference hashes against the seal;
  - `sealed_utc` must be no later than `reference_released_utc`;
  - `reference_answer.json` must hold exactly `{task_id, official_answer}`;
  - no private field names (`gold_answer`, `official_answer`, `reasoning_content`, …) anywhere else.
- Episodes whose task id is in `exclude` (issue #47) are dropped entirely. Only their count is shown to the
  learner. S6's format, which never releases them, is accepted too, including a block whose episodes are
  all withheld.

**Per episode.** Each episode carries:
- the question, the sealed answer and the reference;
- the strict and 1 % grades, computed by the injected grader (production: the pinned `reward.py`);
- the status, transport loss and output tokens;
- the served note ids (from the write-protected marker, else from `summary.json`).

**Model view (`model_view(api_dir)`).**
- It uses the request of every compaction call plus the final call's request and response. So everything
  the model saw appears once, in order, with a `CONTEXT COMPACTION` boundary and the model's own summary.
- Items Codex re-sends after a resume (with fresh ids) are removed by content, against earlier segments only.
- Broker-inserted items are labelled from `controller.json` (for example `HARNESS NOTE (ledger)`).
- A harness re-query is shown as the draft, then the note, then the final message.
- Reasoning items are never rendered. The Codex developer prompt is replaced by a one-line marker.
- Truncation is identical for both learners and is the cards limits:

  | item | kept |
  |---|---|
  | commands | 600 + 1,400 characters |
  | outputs | 1,500 + 500 |
  | model messages and summaries | 3,000 + 1,000 |
  | user messages and notes | 1,500 + 500 |

  This is applied on top of Codex's own truncation, which stays as the model saw it.
- Only the segment requests are held in memory. For a 359-call v8 run the peak is 8 MB and the build takes
  0.5 s.

**Controller view.** The `api/controller.jsonl` rows plus the per-call inserted reasons and verdicts.

**Ledger view** (controller arms). From `ledger.jsonl` and `checks/NNN.json`:
- command counts and the most repeated command;
- harness notes by reason;
- candidates (answer, how it was given, alternative, reading, keep reason);
- calc runs with each input linked to its observation (locator and attributes);
- every gate check (kind, status, fired rules, shown finding with its offer).

**Access.** `Evidence.read(ordinal, part, page)` pages every part by 20,000 characters.
`Evidence.digest` is a sha over everything a learner can see, recorded in `request.json` and
`evidence-index.json`.

### 1.2 Session (`learner_common.run_session`), the same for both learners

- **Tools** (all answered by the controller; the learner has no shell or file tools and writes nothing):
  `list_episodes`, `read_episode`, `search_corpus`, `read_corpus`, `verify_cell`, `verify_answer`, `calc`,
  `propose_note`, plus the mechanism learner's three.
  - `verify_answer` grades against the released reference, controller-side.
  - `calc` is the cards AST whitelist (only arithmetic syntax is allowed).
- **Arguments** are validated with JSON Schema. A tool error goes back to the model as `{"error": …}` and
  the session continues.
- **Budget** (`Budget`, identical for both learners):
  - input 2.0 M tokens, output 120 k tokens, 400 calls;
  - `max_tokens` of each call = the remaining output budget (at most 65,536);
  - the call that crosses a token budget completes, and then the session ends as `budget_exhausted`.
- **Checkpoint.** At 600 k prompt tokens the learner writes its own checkpoint (`tool_choice='none'`) and
  continues from it.
- **Runaways.** An empty output-cap runaway is re-sent unchanged at most 3 times.
- **Nudge.** One nudge when the learner ends with nothing proposed although wrong episodes exist.
- **Records.**
  - `session/control/fireworks-calls/NNN.json` in the cards / `client.complete` record shape, plus
    `reasoning_effort`, `request_max_tokens` and `tool_choice`;
  - `fireworks-tool-results/NNN.json`, `fireworks-result.json`, `prompt.md`, `fireworks-report.md`;
  - provider reasoning is kept only in the in-memory conversation.
- **Provenance.** A response from any other model, or a non-200 status, fails the round.
- **Production model.** `FireworksModel(key, reasoning_effort='high')` calls
  `client.complete(key, messages, tools=…, reasoning_effort=…, max_tokens=…)`. It deliberately does not use
  the unbounded mode, which strips `reasoning_effort`. `reasoning_effort` is a `run_round` parameter, and the
  round fails if the model client's effort differs from the declared one.

### 1.3 Notes, verification, retirement (both evolve arms)

**Format.** A `NoteSpec` with:
- 1-8 lowercase triggers, with no regex, no `'*'`, and a content word of 3 or more letters (so
  "department of the army" is accepted, unlike v8's rule);
- text of at most 600 bytes;
- `admitted_block` and `origin='learned'`, set by the controller.

**Selection.** `select_notes` is the serving order of S6's `notes_render.choose`: matching notes by
(`admitted_block`, `id`), at most 3. A test pins the equality.

**Screen of `propose_note`** (answered at once). It refuses:
- a bad id or an id already used (a screen-rejected id stays free, so a fixed proposal can be re-sent);
- bad triggers or text;
- support outside the pool, or support above 5 questions;
- a support question that does not contain a trigger, or on which 3 older notes would be shown first;
- a leak (`pack.leak_screen` against every released reference, plus the cards 8-word question-copy check);
- a note identical to one already rejected by verification;
- a note that does not fit the shared 16 KB budget.

The budget counts SEED.md, every note as rendered (all notes, not only the 3 shown), the enabled rule
templates and, for controller arms, the controller's loop and enforcement templates. This is the same
formula as S6's `arms.model_visible`. At most 2 notes are accepted per block.

**Pairing** (as in cards). Support and control questions come only from episodes answered under the
current adapter revision (`adapter_since`).
- *Pool*: those episodes that were wrong, unclaimed and not transport-lost.
- *Controls*: right ones whose question matches the triggers first, then the most recent.

**Verification.**
1. Each accepted note alone is added to (source minus notes retired this round).
2. Each support question is run × 2 and each control × 2, by `actor_runner(adapter, task, run_dir)`, up to
   8 at once. A run lost to transport is retried once.
3. Answers are graded controller-side.
4. `judge` is cards `block.judge` unchanged; a test compares it with the v8 file on six run patterns.

**Retirement** (declared rule), computed at the start of each round from the evidence:
- A learned note is retired when it was served on at least 4 later episodes, and its strict accuracy there
  is at least 20 points below the accuracy of the same blocks' episodes where it was not served.
- Transport-lost episodes are excluded on both sides.
- The served ids come from the records; when a record is missing they are re-derived.
- A round with only a retirement gets the status `revised`.

### 1.4 Mechanism admission (zero model cost, during the session)

**`propose_rule(rule, targets)`.** The learner may set only `id, when, finding, offer, priority, triggers`.
The controller sets `origin='learned'`, `admitted_block` and `scope`. Admission needs all of:
1. `validate_rule` over the core + pack vocabulary;
2. the leak screen and 8-word check on the finding, the triggers and every predicate/offer argument, plus
   the text budget;
3. the counterfactual (`replay.counterfactual`, grader injected) passes on at least 1 of the targets;
4. `precision_check` over all released episodes with a ledger (Wilson upper bound of the right-episode fire
   rate ≤ 0.20, at least 10 right episodes);
5. no episode takes more than 2 s to evaluate.

The reply carries only booleans and counts.

**`modify_rule(id, patch)`.**

| patch | admitted when |
|---|---|
| `enabled: false` (alone) | the rule's point right-fire rate is above 0.20 on at least 10 right episodes |
| priority only | the schema check passes |
| anything else | the rule passes the new-rule admission (targets needed) |

Disabling together with other changes is refused.

**`set_policy(key, value)`.**
- Only keys whose range is not fixed.
- Admission: `validate_policy`, and the value must differ from the current one.
- The reply and `mechanical-admission.json` report `policy_replay` over the released harness logs:
  - the loop guard re-run over each ledger (notes and escalations under the old and new policy, changed
    episodes split right/wrong, recorded loop notes);
  - gate decisions re-decided from each `checks/NNN.json`;
  - enforcement rows that would be skipped;
  - ledger splices that would be shortened;
  - keys it cannot replay (`enforce_min_remaining_s`) are listed.

**Limits.** At most 2 rule/policy edits are admitted per block; notes are counted separately.

**Prompt.** The status section shows every rule with its fire counts on the released episodes (computed
without executing offers), the policy with its ranges, and the vocabulary: names, keyword arguments, the
first doc paragraph, and the reference docstrings that list the binding keys (core predicates, core
offers, the pack's rule module).

### 1.5 The round (`learner_common.run_round`) and its records

**Order.**
1. Refuse an existing output directory (raises, as in v8).
2. Validate the source lineage: W0, or the previous candidate's `result.json` receipt (activated, same
   revision, earlier block, same architecture).
3. Build the evidence.
4. Load state and ledger, compute retirement and pairing, write `request.json` and `evidence-index.json`.
5. Run the session and the note verification.
6. Write the new revision `output/adapter`. It is written only if something changed: notes are added or
   removed, and rules and `policy.json` are written.
7. Check the file-level diff against the expected changes, the source and evidence snapshots, and the
   final text budget.
8. Write `mechanical-admission.json`.
9. Commit the state and ledger rows.

**`result.json`.** It keeps the v8 fields (`status, activated, candidate, block, model, profile,
architecture, usage, session`) and adds `learner, reasoning_effort, budget, budget_exhausted,
learner_status, admitted, retired, proposals, verification_episodes, text_budget, evidence_digest, error`.

**Failures are loud.** Any failure gives `status: 'failed'`, the error (with the key redacted),
`activated: false` and `candidate = source`. The state is not written, and in-memory ledger rows are
appended as `void`. Failures covered: transport, provenance, stop event, sealed evidence changed, reasoning
effort mismatch, wrong lineage, no corpus, an unexpected revision diff.

## 2. Verification (exact commands and results)

All commands run from `officeqa-pro133-v9-plumbing/`.

| command | result |
|---|---|
| `$PY -m pytest -q -p no:cacheprovider tests/test_v9_learners.py` | **38 passed, 1 skipped** (5.2 s; the skip is the slow tier) |
| `$PY -m pytest -q -p no:cacheprovider -m slow tests/test_v9_learners.py` | **1 passed** (9.8 s): model view of all 133 v8 none-arm runs |
| `$PY -m pytest -q -p no:cacheprovider tests` | **312 passed, 48 skipped** (84.5 s; the skips are the opt-in slow / live_codex groups) |

One earlier full run showed 1 failure in S6's `test_two_roots_advance_block_by_block` while S6 was editing
its files. The test passes alone, together with the learner tests, and in the two later full runs.

What the tests establish (`tests/test_v9_learners.py`):
- **Evidence.**
  - Synthetic api layouts: compaction segments and ordering, content dedupe of re-sent items, harness notes
    labelled, reasoning and developer prompt hidden, the exact cut lengths.
  - A re-query shown as draft → note → final.
  - Toy campaign roots built by driving `packs/toy/sim.py` through a recorder that writes the broker's
    `api/NNN` files:
    - grades computed;
    - an issue-47-style exclusion drops the episode;
    - the digest is deterministic;
    - pages;
    - no reference values in the controller index;
    - only released blocks are read.
  - Refusals: a sealed prediction changed, a private field, missing `api/`, a missing released block,
    reversed seal times, a `run_dir` escaping the root.
  - A block whose episodes are all withheld is valid.
  - The ledger view links calc inputs to cells and shows gate findings and offers.
  - A copied (relocated) root works: v8 summaries carry absolute `records` paths, so resolution now prefers
    in-root locations.
- **Real v8 records** (read-only): for all 133 none-arm runs the model view agrees exactly with
  `controller_runtime.replay_run`: compactions (35 in total), calls, and commands excluding `write_stdin`
  polls (3 runs have 1-2 polls, which the model did see). The developer prompt is shown once and every
  output is within the cut. Timings: 10.3 s for 133 runs; trajectories are 32,849 characters at the median
  and 1,169,241 at most (59 pages).
- **Shared pieces.**
  - `calc` refuses non-arithmetic input.
  - Trigger rules, and the selection order equal to S6's `notes_render.choose`, and `render_note` equal.
  - `judge` equals v8 `block.judge` on 6 patterns.
  - The issue-47 list equals `cards_learning.EXCLUDED_TASKS` (read with `ast`, not imported).
  - The text budget parts.
  - `FireworksModel` passes `reasoning_effort='high'` and `max_tokens`, not the unbounded mode, and never
    shows the key in `repr`.
- **Text learner end to end** (scripted model, fake actor):
  - Every common tool is answered, and a bad tool name and a schema violation come back as errors.
  - A leaking proposal and an out-of-pool proposal are refused. The fixed proposal is re-sent under the
    same id and accepted.
  - 8 verification runs (support 1 and 7 × 2; controls 4 and 10 × 2), each on the note-only trial adapter.
  - The note is admitted into `output/adapter/notes/`.
  - State: claimed support, `adapter_since` = b+1. Ledger rows are written.
  - `mechanical-admission.json` has real values.
  - The call record shape is right, and the private reasoning appears in no file.
  - Also: a failing verification is rejected (status `retained`, sha recorded), and rounds chain through
    the previous candidate's receipt while a tampered candidate is refused.
- **Retirement.** A note served on 4 later episodes with 0 right, against 5/6 without it, is retired
  (status `revised`). A note served on 3 is kept. Served ids are re-derived when unrecorded.
- **Session loop.** The call, output-token and input-token budgets are each exhausted at exactly 3 calls,
  with per-call `max_tokens` of 500 → 300 → 100. 3 runaway re-sends; a checkpoint with a reset
  conversation.
- **Loud failures** (7 cases). An unexpected extra file in the revision fails the round, sets
  `operation_provenance_matches: false` and voids the ledger rows.
- **Mechanism learner** (30 gated toy episodes with the core seed rules, real sandbox runner):
  - `rule_stats` equals `replay.fire_stats` for 6 rules.
  - A learned `later_printing` rule is admitted: counterfactual on targets 1 and 7; 0/21 right fires and
    6/9 wrong fires; bound 0.155.
  - A noisy rule fails precision and the counterfactual. An unknown predicate and a leaking template are
    refused.
  - At block 1, "only 4 right episodes (< 10)".
  - Disabling a rule with 3/21 right fires is refused. A priority change is admitted. Out-of-range and
    unchanged policy values are refused; `loop_threshold=4` is admitted with its replay. A third edit is
    refused.
  - The gate-rounds replay: `gate_rounds_max=0` suppresses every shown finding.
  - The loop-guard replay counts notes and escalations, and ignores polls.
  - End to end: a rule, a policy change and a verified note are admitted into one revision with the exact
    expected diff. A transport failure after an admitted rule voids its row.
- **Equal inputs.** On the same root, the text and mechanism learners get byte-identical tool results for
  the same evidence reads, and the same evidence digest, episodes, pool, right set, budget, reasoning
  effort, model and verification constants. The mechanism tools are strictly extra.

## 3. Deviations from the brief (declared)

1. **Evidence validation** accepts any number of episodes per block (v8 required exactly 5), and a block
   whose episodes are all withheld. The seal is read from `<root>/private-learning-seals/`, not the module
   directory.
2. **Run directory resolution:** the manifest `run_dir` (an escape is refused), else the first in-root
   location of `[summary.records parent, runs/adapted/NNN]` that holds `api/`.
3. **Note selection order** follows S6's serving order (`admitted_block`, `id`), not trigger specificity, so
   the learner screens against what is actually served.
4. **`SUPPORT_MAX = 5`** support questions per note. This is a new cost bound; cards had none.
5. **Learned notes may not use `'*'`** and need a content word in every trigger (they must be scoped).
6. **Status `revised`** for a round whose only change is a retirement.
7. **A note is verified on the source adapter alone** (minus notes retired this round). Rules and policy
   admitted in the same block are *not* in the trial, as in cards, which verifies each card alone.
8. **Rule disabling** is admitted only on a measured point right-fire rate above 0.20 with at least 10
   right episodes. A priority-only change needs only the schema. Both are declared admission rules the
   brief left open.
9. **The corpus falls back** to `TextDirCorpus` when a root has a corpus but no FTS index (toy roots).

## 4. Gaps and risks

1. **Budget values are a [user] decision.** 2.0 M input tokens is about the v8 cards median per block, so
   about half of the v8-like sessions would stop at the cap. `reasoning_effort='high'` will raise completion
   tokens relative to v8, which used the provider default.
2. **There is no retirement for learned rules.** A learned rule that later turns noisy stays until the
   learner disables it (whose admission needs measured noise). The mechanism status section shows every
   rule's current fire counts, so the learner can see it.
3. **The union fire-rate limit is not checked at admission.** Per-rule precision is checked, but the
   pre-registered union right-fire limit (≤ 20 % over all rules) and the flip limit are not enforced by the
   learner. They are pilot and audit numbers.
4. **The optional decision-point replay** (DESIGN §3, about $1-3 per rule) is not implemented.
5. **Controller-side cost of mechanism admission.** Each `propose_rule` evaluates the rule on every
   released episode with offers in the sandbox. The OfficeQA pack's predicates were measured by S2 at up to
   0.8 s per check, so one proposal can take about 1-2 minutes of controller time late in a campaign. The
   status section's fire counts (computed without offers) cost about the same once per round. The model
   waits meanwhile; no tokens are spent.
6. **The fake model is not Fireworks.** The production path (`FireworksModel` → `client.complete` with a
   string `reasoning_effort`, `max_tokens` and `tools`) is checked only by argument capture. Whether GLM on
   Fireworks accepts `reasoning_effort='high'` on chat/completions together with tools is to be confirmed in
   the paid smoke (preflight learner smoke).
7. **Toy-only end-to-end.** No OfficeQA evidence was built with real references (by rule). The OfficeQA pack
   path is exercised only through the vocabulary, leak screen and default-exclude tests.

Requests to other owners: `officeqa-v9-design/wave2/S5-learners-requests.md`.
