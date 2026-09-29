# S8-e2e: requests for other owners

S8 fixed the integration defects it could reproduce, inside the v9 files (listed in `S8-e2e.md` §3). The items
below are changes S8 did not make: documentation owned by others, design choices, or work beyond "minimal".
None blocks a test; the whole suite passes. Paths are relative to `officeqa-pro133-v9-plumbing/`.

## S6 (orchestration docs: PROTOCOL.md, PREREG.md)

1. **Audit wording for learned rules.** `full_audit.admitted_rules` now judges each round's served rules against
   the revision served *before that round* (W0 for round 1), with the record of the admission mode S5 uses:
   - `full` (new rules, and every modification other than the two below): counterfactual + precision numbers;
   - `priority`: the rule differs from its predecessor only in `priority` (and `stats`);
   - `disable`: the predecessor with `enabled: false`, admitted on a measured right-episode fire rate above
     `MAX_RIGHT_FIRE` on at least `MIN_RIGHT` right episodes.
   Policy values are compared with the previous revision too. PROTOCOL.md §3 still says "every served rule/policy
   that differs from W0 admitted with replay numbers". Please update it (and PREREG if it quotes the audit).
2. **Pack-neutral orchestration is partial.** For a second pack (SpreadsheetBench confirmation), these are still
   OfficeQA-only:
   - `data.py`: `verify_prepared` requires 133 tasks, the v3-v8 selection seed and the pinned `reward.py`;
     `manifest_v9` texts; `ISSUE_47` as the withheld list; the grading labels ("OfficeQA Pro").
   - `campaign.COMMON` ("# OfficeQA task workspace") and `HELPER_README` + `officeqa.py` (the FTS helper) are in
     every pack's workspace.
   - `arms.W0_NAMES` is one directory per seed kind, so a code copy holds the W0s of one pack only (the toy
     rehearsal builds its own code copy with `arms.build_w0(..., ToyPack(), ...)`).
   Suggestion: a pack attribute for the workspace intro and helper, and a pack-aware prepared-data verifier.

## S5 (learner_common.py)

3. **Opaque verification run directories** (repeat of S6-review request 1). The rehearsal confirms that every
   verification episode shows its model the path `<root>/evolution/01/verify/<note id>/q<NNN>-k<k>/workspace`
   (Codex `<cwd>` and the AGENTS.md header), i.e. that it is a verification run and which note is tested. It is
   symmetric between the evolve arms, but it makes verification runs differ from the scored runs they predict.

## S3 / broker owner (broker.py)

4. **A stalled provider stream outlives its episode.** With `BROKER_OPTIONS['request_timeout'] = None` the broker
   reads the upstream stream without any socket bound. The rehearsal stalls one provider response: the actor is
   killed at its wall clock (correct: status `timeout`, empty prediction, no answer fallback), but the broker's
   handler thread stays blocked on the provider until the provider closes the stream. Only then is
   `api/NNN/transport.json` written and `api/summary.json` refreshed. In the test that is the end of the test;
   in a paid run it can be minutes or hours later, after the block was sealed and released, possibly after the
   final audit.
   - Consequences: a thread + socket per stalled call for the rest of the process. Records of a sealed episode
     also change late: the episode `summary.json` keeps its snapshot (`usage_complete: false`) while the api
     records are completed later.
   - What is already right: the broker never re-sends after its episode (the stop event is checked before every
     dispatch). The late record says "usage unknown", never zero.
   - Suggestion (S3 wave-1 gap "per-call wire deadline tied to actor_deadline"): on broker exit, close the active
     upstream response (`broker.active_response`), so the handler ends with the episode and writes its record
     before the episode summary is taken.

## Contracts owner (harness_core/contracts.py; documentation only)

5. `Pack.usage()` is required by orchestration for every pack with L1 tools (`campaign.compose_readme`). The kit
   wiring convention (a module-level `tool_wrappers()` in `packs/<name>/pack.py`, used by
   `campaign.tool_wrappers`) is not in the contract. Please document it, or add `tool_wrappers` to the Pack
   protocol.

## Orchestrator / user (declare before the paid smoke)

6. **Codex's environment context is false about the sandbox.** Every request tells the model
   `<permission_profile type="disabled"><file_system type="unrestricted" />`. The v9 Seatbelt profile denies
   everything outside the workspace and the granted roots; the rehearsal's probe gets "Operation not permitted"
   five times out of five. `include_permissions_instructions=false` removes only the developer permissions
   block, not this line. The text is identical in every arm, so it is not a fairness issue, and README.md states
   the real rule. Declare it in PROTOCOL.md as a known model-visible inaccuracy.
7. **cost.py after a transport fault.** Any HTTP 5xx, dropped stream or stalled call makes that group's
   `estimate_usd` None, because unknown usage is never priced as zero. The paid smoke's cost check should read
   `known_cost_lower_bound_usd` together with `unknown_usage_logical_calls`.
