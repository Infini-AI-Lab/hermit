# v9 design review — engineering feasibility and risk

Lens: DESIGN.md (draft 1) checked against the v8 code in `officeqa-pro133-v8-plumbing/` (`P/`) and the Codex
behaviour in `understand/*.md`. No answers or graders were opened. Note that `officeqa-pro133-v9-plumbing/` already
exists and is byte-identical to `P/` (checked with `cmp`), so the copy in build step 1 is done.

**Verdict.** The design can be built. The work is about 5 k new or changed LOC (lines of code) plus about 2.4 k LOC of
tests, roughly v8's size again. Three choices should change before coding (#1–#3). Most checks that seem to need a
live Codex can be run for $0 with the real binary and a fake upstream (#4).

## Ranked problems

**1. A ledger kept inside the sandbox sees only the L1 tools, so L2 and L4 work from partial data.**
- L0 makes `rg`, `python3` and heredocs work. v8 tried `rg` in 121–133 of 133 runs even while it was broken, so the
  model will use these raw tools.
- Cells read with `rg`, `sed` or `python3 -c` never reach `.officeqa-ledger/`.
- An in-sandbox `submit` cannot see any earlier output: the run is `--ephemeral`, so there is no session file.
- As a result, `cell_exists` calls numbers the model did read "typed from memory?". The other four content checkers
  match no cells. Their nudges then land on right answers.
- *Fix:* the broker derives the ledger from the `function_call` / `function_call_output` items it relays.
  - These are exactly what the model saw.
  - The loop guard needs a command log that survives compaction anyway; the ledger comes from the same log.
  - L1 tools print a parseable tag per cell, e.g. `⟦c17 FFO-3 2004_03:741|2003-01|GSA|1234 r⟧`.
  - `submit` becomes a thin CLI. It POSTs to a new broker route `/v1/submit`, following the `/v1/local-mwm` precedent
    (`broker.py:228-242`, which takes no response lock), and prints the findings it gets back. The feedback still
    arrives as tool output.
  - The broker saves each checker input to `api/checks/NNN.json`. Admission replays those same inputs, so online and
    offline behaviour match by construction.

**2. Controller reads of an actor-writable file can leak the answer key.**
- The unsandboxed controller reads `<ws>/.officeqa-ledger/ledger.jsonl` in four places: the post-compaction splice,
  loop-note candidates, `fallback_answer`, and the evidence copy.
- The actor can replace that file, or its directory, with a symlink to `../../../../prepared/private/answers.jsonl`.
  Creating the symlink needs no read right on the target.
- The broker would then render the target into context, and `shutil.copytree` would copy it into learner evidence.
- *Fix:* #1 removes these reads. If the file stays:
  - open each path part with `O_NOFOLLOW` and check it is a regular file under a size cap, as `collect_final` does
    (`P/acting.py:384`);
  - re-serialize rows through a schema whitelist;
  - add a unit test with a planted symlink.

**3. The "rare" submit-enforcement path will be common, and it is the hardest path.**
- Nothing makes GLM call `submit`. Text procedures were followed fully in only 2 of 32 cases (FINDINGS §2.2).
- When the model has not submitted, the re-query applies. If its revision is a tool call, the draft and the note must
  be spliced into every later request (broker.md §3c, test 11). This is the least-tested path.
- Comparing free text also fires on formatting alone, e.g. "$1,234.5 million" vs "1234.5".
- *Fix:*
  - fire only if there was no submit, or if the numeric tokens differ;
  - put the draft's checker findings (computed in the broker, per #1) into the re-query, so a plain final message is a
    valid revision;
  - cap at 1 per run, and skip if fewer than 60 s remain;
  - in the pilot, report the submit rate and the re-query rate as go/no-go numbers.

**4. Codex behaviour is only verified in the paid step 6, although most of it can be checked for free.**
- Run the pinned `codex exec` against the broker and a fake upstream: no key, localhost only.
- This settles:
  - whether Codex accepts a forwarded revision, and whether `final.md` then equals the revision;
  - whether a message-only reply ends the turn;
  - whether Codex re-sends identical input after `event: error`;
  - what request 001 contains with `include_permissions_instructions=false`;
  - the truncation size;
  - the actual `ZDOTDIR`, `TMPPREFIX` and PATH inside `exec_command`;
  - whether Codex starts without `cua_node` or the venv;
  - whether the compaction splice lands correctly. Forcing compaction by reporting inflated usage is itself
    unverified; a low limit is the fallback.
- The fake must first emit `output_item.done` events. Today's fake does not (`tests/test_v8_broker.py:34-39`).
- *Fix:* make tier 1 below the exit gate of build step 2.

**5. Silent failures turn an evolve arm into a static arm.** A learner that fails returns `candidate = source` and
does not raise. Four ways this can happen:
- (a) `safe_file` rejects hidden path parts (`P/learning.py:155`). Evidence copied from `.officeqa-ledger/` would fail
  validation in every block.
- (b) The file-tool fix is inferred, not reproduced. Python's import finder must list the framework directory, which
  `P/full_sandbox.py:15` removes. Grant read on a directory that holds only `harnessadapter`.
- (c) `client.py:43-45` strips `reasoning_effort`. Whether chat/completions accepts `'high'` for GLM is unknown.
- (d) `preflight.py:67` requires byte-equality with v6f, so it breaks once these fixes land.
- *Fix:*
  - store evidence under non-hidden names (`NNN/ledger.jsonl`);
  - add a preflight learner smoke: build and validate synthetic evidence, then read, write and validate a file through
    the helper;
  - make full_audit fail on any round with status `failed`.

**6. Learner checkers inside the actor sandbox are unsafe and form an extra text channel.**
- Nested Seatbelt profiles are impossible (`P/acting.py:194`), so "no network" cannot be enforced there, and the broker
  port is reachable.
- The actor can read checker source and `policy.json`.
- Finding text has no cap, so 8 × 4 KB of checkers can carry keyword-triggered advice outside the 16 KB budget.
- *Fix:* the controller runs checkers.
  - One subprocess per call, under its own `sandbox-exec` profile: no network, read access only to the checker and
    its input.
  - Empty environment, `RLIMIT_CPU=2`, killed after 5 s of wall time.
  - Findings are templates of at most 200 B that cite ledger values. Templates count toward the 16 KB budget.

**7. The cross-root barrier has no failure handling.**
- Each root is a separate process with its own module-level config (`P/paths.py:7-9`), so the in-process
  `threading.Barrier` (`campaign.py:350`) does not carry over.
- v8 cannot resume a campaign, so one crash either deadlocks the other 8 roots or ends the run.
- *Fix:* a new `barrier.py`.
  - Each root writes an atomic `<coord>/block-NN/<root>.done` file and polls for the others, honouring `stopping`.
  - A timeout writes an `ABORT` file, and every root saves its state.
  - Add block-level resume: move the partial block to `runs-aborted/` and reuse the freeze.
  - full_audit checks that start(b+1) ≥ max end(b).

**8. Arm plumbing and audit invariants.**
- An unknown architecture becomes `mwm` (`experiment.py:12`, `full_audit.py:18`).
- Five arms do not fit `{baseline, adapted}`.
- Controller calls must copy the parent request's `native_request_settings`. Otherwise `full_audit.py:56-58` and
  `run.py:47-49` fail.
- A `tools=[]` wrap-up call is miscounted as a compaction.
- *Fix:*
  - add an `ArmSpec{kit,l1,controller,learner,notes}` in `.task-paths.json`, parsed once in `arms.py`;
  - add `origin`, `kind` and `parent` to `transport.json`;
  - record `codex_input_sha256` on every call.
  - Consider dropping wrap-up: 0 of 386 completed runs ended in their last 300 s (critic N3).

**9. Per-episode AGENTS.md.**
- The question is not available at `prepare_workspace` (`campaign.py:120-121`).
- *Fix:*
  - choose notes in a new `notes_render.py` before the marker hash is computed;
  - use keyword triggers only: learner regexes risk catastrophic backtracking;
  - fix cards' short-token rule;
  - record the chosen note ids; full_audit re-derives them.

**10. Smaller items.**
- The python prefix is 422 MB. Keep one frozen copy outside the roots, hash it by manifest, and test that it still
  works after moving.
- Add the tables index to the read roots and open it with `mode=ro&immutable=1`.
- `submit` has no access to the question.
- `feedback_source` (`acting.py:480`) no longer holds for mech arms.
- Storage is about 40 GB (200 GB free).
- A file ledger needs `flock`, because tool calls can run in parallel.

## What changes, per layer

| Layer | Files / functions | Breaks if wrong |
|---|---|---|
| L0 | `acting.py` env 166-174, roots 136-144, protected 153/215, read roots; new `kit.py`; kit in `freeze` | Codex start-up, PATH |
| L1 | `tools/*`; index in `officeqa_prepare.py`, `data.verify_prepared` | read-only sqlite |
| L2/L3 | `broker.py`: `do_POST` 254-446 → reusable `_call()`, splices, `/v1/submit`, deadline; new `controller.py`, `transcript.py` | cache prefix, retries, audit |
| L4 | `checkers/runner.py`, profile, seeds, `replay_checkers.py` | online ≠ offline |
| L5 | `notes_render.py`; 16 KB cap in `full_learning` 262-269 | marker hash |
| Learners | `full_sandbox.py`, `client.py`, `mechanism_learning.py` (cards skeleton), `admission.py`, leak screen from `block.py:25-128` | silent no-learning |
| Campaign/audit | `experiment.py`, `campaign.py` (336-339, 364, 390, 170-276, 426-437), `data.py`, `barrier.py`, `full_audit.py`, `preflight.py` 33/67/92, `cost.py` (split by `origin`), `run.py`, PROTOCOL.md | audit passes on a stale protocol |

## Minimal smoke plan

**Tier 0: offline, $0.**
- (1) `probe9.sh` under the v9 profile with the copied python and rg.
- (2) Kit tools on the real corpus inside the profile; pages ≤ 8,000 B.
- (3) The learner helper can read, write and validate.
- (4) Evidence is assembled and then validated.
- (5) Checker runner: network denied, outside reads denied, 2 s CPU kill, no key in the environment.
- (6) The symlink test, plus the replay goldens (22/25/29 loop runs, 35/38/46 compactions).

**Tier 1: real Codex, broker and fake upstream, $0.**
- (7) With `controller=None`, upstream bodies are byte-equal to v8.
- (8) The upstream input of request N+1 extends that of request N.
- (9) The gate forwards the revision and `final.md` equals it. A tool-call revision is executed, then spliced.
- (10) A retry after an error sends the identical body, with no duplicate note.
- (11) A forced compaction gets exactly one frozen ledger splice.
- (12) Request 001 with and without the permissions key; truncation size; the `exec_command` environment.

**Tier 2: paid, about $1.**
- (13) Fireworks accepts echoed reasoning items and a mid-history user note. Record the event-type skeleton of the SSE
  (server-sent events) stream.
- (14) One learner call with `reasoning_effort='high'`.
- (15) A marker task passes the gate and triggers no loop note.

Then run the 20-question pilot with the go/no-go numbers from #3.

## Size and parallel split (disjoint files per stream)

| Stream | Owns | LOC + tests | Days |
|---|---|---|---|
| A Contracts (first) | `contracts.py` (ArmSpec, record schemas, cell-tag grammar, checker API, `Broker` kwargs), `arms.py`, `experiment.py` | 200 + 100 | 1–2 |
| B Env + tools | L0/L1 rows (`acting.py`, `kit.py` takes `prepare_workspace` out of `campaign.py`) | 900 + 400 | 5 |
| C Broker | L2/L3 row + `tests/fake_upstream.py`, `codexsim.py`, tier 1 | 1,100 + 900 | 7–8 |
| D Checkers | L4 row | 700 + 300 | 4 |
| E Learners | Learners row + `postmortem.py` | 1,200 + 400 | 7 |
| F Orchestration + audit | Campaign/audit row + `notes_render.py`, `report.py` | 900 + 300 | 5–6 |

Seams are only A's contracts: C↔D via `/v1/submit` → `runner.run(input_json)`; B↔C via the cell-tag grammar; F builds
`Broker(..., controller, deadline, question)`. Critical path A → C → tier 1 → pilot: ~2 weeks with 3–4 engineers.
