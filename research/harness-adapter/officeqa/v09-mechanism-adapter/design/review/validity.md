# Review of DESIGN.md draft 1: scientific validity and fairness

Lens: would this convince skeptical researchers that *evolving a harness adapter* beats *evolving a prompt adapter*
(GEPA/SkillOpt-style) and beats a plain harness? Sources: DESIGN.md, FINDINGS.md, `index.json` (0/1 flags only) and
`understand/{campaign,critic,replay}.md`. No answers or graders were opened.

**Verdict.** As written, a positive primary result would not convince a skeptical reviewer, for four reasons:

1. The primary contrast gives "evolution" the credit for hand-built machinery.
2. The test set is also the development set.
3. The prompt baseline lacks the selection step that makes GEPA work.
4. With 3 repeats the test can only detect a gap of about 10 questions, but the evidence supports a gain of 5 or fewer.

The fixes below mostly cost about 3 extra static campaigns and one held-out evaluation.

Terms used below:
- **Prequential**: each question is scored once, then used for learning.
- **DiD** (difference-in-differences): the gain from evolution in one arm family minus the gain in the other.
- **MDE** (minimum detectable effect): the smallest gap the test finds 80% of the time.
- **GLMM** (generalised linear mixed model): a logistic regression with random effects, here for question and campaign.
- **Gatekeeping**: hypotheses are tested in a fixed order at full α, and testing stops at the first failure.
- **TOST** (two one-sided tests): a standard equivalence test.

## P1 (fatal): the primary contrast does not isolate the claim

- §0 says an evolver that edits mechanisms "improves accuracy **more**". That is a claim about *gains*, but the test
  compares *levels*.
- §1 says the contrast "measures only what the evolver may edit". That is false. Only `mech-evolve` has the L2–L4
  runtime:
  - the ledger splice;
  - the loop guard;
  - wrap-up;
  - the `submit` gate with 6 seed checkers;
  - notes retrieved by trigger.
- A reviewer will say that `mech-static` alone explains the gap. The design runs `mech-static` once, descriptively, so
  it cannot rule this out.

**Fix.** Use a 2×2 factorial design, {text, mech} × {static, evolve}, plus `plain`:
- Redefine `base` as **text-static**, with the seed-parity text from P2 as its starting adapter (W0).
- Give `prompt-evolve` the same scoped-note format: triggers, at most 3 notes served, 16 KB total. SkillOpt skills are
  scoped anyway, and FINDINGS §2.2 shows that serving the whole deck overreaches.
- Restate H1 as a system-level claim: "a harness adapter beats a prompt adapter with the same actor, learner, tools and
  seed knowledge".
- Add H2: `mech-evolve > mech-static`. This is the test that earns the word *evolving*.
- Report the DiD descriptively. At this budget it is too noisy to be confirmatory.

## P2 (fatal): test-set exposure is uneven and cannot be repaired inside the 133

- L1 exposure is shared between arms, but the L2–L4 seed was built from labelled analysis of these questions:
  - checker choice (`answer_count`: "2/2 wrong, 0 right");
  - a question-spec regex written after reading the 133 question texts (replay.md §12);
  - loop goldens taken from v8 runs.
- 71 of 133 questions were analysed with labels. The other 62 were selected because all v8 arms agreed on them (33 all
  right, 29 all wrong), so they are no clean held-out set (critic R1).

**Fix.**
- **(a) Separate development from test.** Treat Pro-133 as the development and training set. For the confirmatory test,
  freeze and hash all final adapters (3 per evolve arm) and the static arms, then evaluate them on an **unanalysed set**:
  - either non-Pro OfficeQA, prepared by script with answers never viewed (a user decision);
  - or a second document-QA benchmark.

  This is the train→test protocol GEPA and SkillOpt use. It also answers the objection that the online regime
  handicaps text optimisers.
- **(b) Seed parity.** Write the 6 checker rules, the loop advice and the provenance advice as W0 text, no longer than
  the mech seed's model-visible text. Serve it as the starting text for `base` and `prompt-evolve`. Both families then
  start from equal developer knowledge, and text-static vs mech-static is a clean test of "same content, executed vs
  read".
- **(c) Exposure check.** On Pro-133, pre-register an exposure × effect interaction: analysed 71 vs unanalysed 62.
- **(d) Pilot.** Run the step-7 pilot off the confirmatory set, and log every change made after it.

## P3 (major): `prompt-evolve` is not a credible GEPA/SkillOpt stand-in

- The v8 learner rewrote the adapter once per block. It had no measured selection and no rollback, and 26 of 26 rounds
  were admitted.
- It tied `none` (64 vs 62), and part of its gain was an environment workaround that L0 now removes (critic N1).
- `mech-evolve` gets a label-backed admission filter, while text gets none. So "unverified in both" is not equal
  treatment. Selection on measured performance is GEPA's defining ingredient.

**Fix.**
- **Verified admission for text.** Admit prompt notes only after paired re-runs, reusing the cards machinery: 2 runs per
  support question plus 2 controls. This cost $13 for 240 runs in v8.
- **Retirement.** Retire any note whose results on later matching questions (prequential hits) fall below its matched
  controls.
- Report that mech admission needs zero rollouts. That is a fair efficiency point, and it keeps the baseline strong.
- **Better, if feasible:** add the official `gepa` optimiser as a 6th arm on the P2 train→test split, with the same
  rollout budget.
- Never place in-house numbers next to SkillOpt's published OfficeQA numbers. SkillOpt uses its own exact-match (EM)
  scorer, not `reward.py`.

## P4 (major): learner information and budgets differ

- **What each learner sees.**
  - The mech learner sees **all** released episodes, merged with ledger and controller logs.
  - The v8 prompt learner sees only its current block, with full `aggregated_output` (campaign.md §2.2), plus
    OfficeQA-specific LADDER S1 (a vintage rule) and a reference shelf.
- **Text budget.** Prompt gets 16 KB of notes. Mech gets 16 KB of notes, 8×4 KB of checker code, and policy wording.
  Checker message strings are a second, unscreened text channel that reaches the model at submit time.
- **Extra runtime in mech arms only.**
  - Splice, gate and wrap-up tokens (critic R4).
  - Wrap-up changes the *official* answer. In v8, 13 of 399 runs timed out, about 4 per arm-run, which is the scale of
    the effects being tested. Wrap-up is also not evolvable.

**Fix.**
- Build the evidence for both learners with one builder: all released episodes, the same merged view and the same
  truncation.
- Give both learners identical domain text: strip S1 and the shelf, or give them to both.
- Count every model-visible string (notes, checker messages, nudges) against one shared 16 KB budget, and leak-screen
  those strings.
- Cap the learner by tokens, not by calls.
- Move wrap-up into L0 for every arm.
- Report broker-measured tokens and $ per question for each arm.

## P5 (major): power and the unit of replication

- **The test is anti-conservative.** Sign-flip over questions assumes independent questions, but every later question
  in an evolve campaign shares that campaign's adapter.
- **Too few campaigns.** An exact 3 vs 3 campaign-level test has 20 relabellings, so the smallest possible two-sided p
  is 0.10.
- **The MDE is larger than the expected effect.**
  - v8 discordance was 34–37 questions, so the SD of one paired difference is about 6.
  - With 3 repeats the SE is about 3.4, giving an MDE of about 10 questions, before adding campaign variance.
  - A perfect loop guard is worth about +5 per run. Only COUNT separated right from wrong runs, and it fired on 2 of
    399.
  - The likeliest outcome is an inconclusive null.

**Fix.** Hash and commit these in PROTOCOL.md before the first paid call:
- (a) Primary analysis: a GLMM `y ~ arm + (1|question) + (1|campaign)`, or a two-way bootstrap. Keep the sign-flip as a
  robustness check.
- (b) State the MDE, and read a null result with TOST using ±5 questions.
- (c) Randomise question order **per repeat**, the same across arms within a repeat so the block barrier still pairs
  them.
- (d) On the held-out set, run each frozen adapter twice per question.
- (e) If the pilot shows real campaign variance, use 4 repeats or a pre-registered two-stage sequential design
  (O'Brien–Fleming).

## P6 (moderate): the user wants three claims, but the design tests one

- "Beats plain" rests on one descriptive run.
- "Evolution matters" is never tested.

**Fix.**
- Gatekeep at α = 0.05 in this order:
  1. H1: `mech-evolve > prompt-evolve`;
  2. H2: `mech-evolve > mech-static`;
  3. H3: `mech-evolve > plain`.
- Run `mech-static` ×3 and `plain` ×2. These arms have no learner and cost about $25–35 each at v8 prices.

## P7 (moderate): issue-47 questions

- 18 questions have disputed answer keys. In v8 each arm got 6–8 of them right.
- The `later_printing` checker targets exactly these vintage forks (choices between printings of a table), so a careful
  arm can be penalised systematically.
- The §3 exclusion cannot be enforced for text: a text learner still reads the released references.

**Fix.**
- Keep strict scoring on all 133 questions as primary, and require the same sign on the 115 undisputed questions.
- Withhold issue-47 references from both learners.
- Report the 1% tolerance and fallback metrics as secondary.

## P8 (what a top-venue reviewer rejects outright)

- **One benchmark and one actor.** Evaluate on a second domain (P2a) or with a second actor model, or scope the title
  down.
- **No pre-registration artefact.** Commit and hash the plan before any paid call.
- **An unfalsifiable framing.** If H2 fails, the honest claim is "hand-designed mechanisms beat automated text
  evolution". Pre-declare that wording now.

## Minimal revised matrix

| arm | Starting setup | repeats | role |
|---|---|---|---|
| `plain` | L0 + wrap-up | 2 | H3 |
| `text-static` (was `base`) | L0+L1 + seed-parity text | 1–2 | factorial cell |
| `prompt-evolve` | same seed; scoped notes; verified admission | 3 (4) | H1 |
| `mech-static` | L0–L4 seed | 3 | H2 |
| `mech-evolve` | L0–L4 seed + evolver | 3 (4) | H1–H3 |

**Cost.** Pro-133 campaigns become the development set and secondary results, at roughly +$100–150. The confirmatory
test runs the frozen adapters on the fresh set, at about $0.2–0.3 per actor run. Decide on the held-out set first; the
rest of the plan follows from that decision.
