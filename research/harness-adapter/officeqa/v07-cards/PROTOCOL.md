# OfficeQA Pro 50 — cards adapter, verified by paired actor re-runs (v7)

Two paired arms answer the exact same ordered 50 questions with the same pinned
dataset, 697 parsed TXT documents, retrieval helper, native Codex CLI and the
exact Fireworks `accounts/fireworks/models/glm-5p3-flash` model:

- A (baseline): GLM + native Codex, no adapter.
- B (adapted): GLM + native Codex + the `cards` harness adapter, starting from
  the framework's generic empty deck (W0). `learn_every = 5`.

Inference policy, sandbox and answer release are those of v6f, unchanged: a
3,600 s wall clock per question for both arms; no token, request or cost cap;
Codex retries with accounting (`request_max_retries = 100`,
`stream_max_retries = 100`); `project_doc_max_bytes` 4 MiB; both arms advance
one five-question block at a time; the learning round after Q50 is skipped;
five adapted submissions are sealed before their five reference answers and
strict/1% grades are released to the learner; the learner never sees baseline
traces, unreleased answers or future questions.

## What changes in v7: the adapter and how it learns

The adapter is a deck of cards. A card is one rule: `scope` (keywords that
mark tasks it applies to), `claim` (the rule, at most 600 bytes) and `unless`
(when not to apply it). The deck the actor reads is the fixed preamble plus
every admitted card, compiled by the adapter's own renderer into at most
16,384 bytes (`harnessadapter/architectures/cards`). The preamble is deck
mechanics only (what a card is, follow a matching card, the more specific one
wins); it carries no answering rule, so W0 is an empty deck and every
task-relevant instruction the adapted arm ever receives is an admitted card.

The learner also receives a fixed, host-written description of the
environment (`cards_domain.md`: corpus layout, the actor's tools and sandbox
quirks, the grader's format rules, the benchmark's stated conventions and the
diagnosis-ladder vocabulary). It contains no conclusions about particular
questions and no findings from earlier campaigns; its hash is frozen with the
campaign and recorded in every block's `request.json`.

After each sealed block the controller:

1. grades the block with the pinned official `reward.py` (strict, 1%);
2. records, for every card already in the deck, which of this block's
   questions its scope matched and whether they were answered correctly
   (a prequential record: predicted first, revealed after);
3. runs one GLM learner session over the released episodes (questions,
   submissions, references, grades, public trajectories, failure records), the
   corpus, `verify_cell`, `verify_answer`, and an arithmetic `calc`; it also
   sees the text of every admitted card, the ledger of earlier proposals and
   the previous block's learner report. The learner writes nothing; it may call
   `propose_card` and at most 2 proposals per block pass the screen: valid
   card format, support of at least 1 pool question, no question ids, corpus
   file names, numbers equal to a released reference answer (for the support
   questions any number, spelled out or not), other large numbers, eight-word
   runs copied from a question or Markdown structure, room in the deck's byte
   budget, and not identical to a card already rejected. A learner request is
   bounded at 3,600 s so a dead connection cannot freeze the campaign;
   generation is never cut short;
4. verifies each screened proposal by re-running the actor, with the proposal
   added to a copy of the current deck, on its support questions and on 2
   control questions, each 2 times, eight actors at once, under the same
   runner, sandbox and 3,600 s wall clock as the scored runs
   (`evolution/NN/verify/`). Pairing: support and control questions are drawn
   only from questions whose original scored attempt used the current deck
   (`deck_since` in `cards-state.json`), so the re-run differs from the
   original attempt by the proposal alone; older wrong questions stay visible
   to the learner as evidence but cannot be support or control. Controls are
   the scope-matched right questions first, then the most recent. The card is
   admitted when the support runs miss at most (support count − 1) times in
   total (one support question must be right in both runs), no control
   question is wrong in every run, and the mean output tokens of the
   verification runs are at most 3x those of the original attempts. A
   wall-clock kill is the actor's timeout (strict 0). A run lost to transport
   is retried once; a question still short of 2 valid runs makes the card
   inconclusive (not admitted, not counted as rejected);
5. assembles the next deck from the current deck plus the admitted cards
   (`evolution/NN/adapter`, committed), or keeps the current deck unchanged
   when nothing was admitted; every proposal, run and verdict goes to
   `cards-ledger.jsonl`; claimed questions and rejected proposals to
   `cards-state.json`.

Questions the benchmark's issue tracker lists as questionable
(databricks/officeqa#47: UID0155, UID0213, UID0237) are scored like every other
question but never used as support or control.

## What the result can and cannot show

Admission shows that a card, once delivered, works on the questions it was
written for without losing control questions; it does not show
generalisation. Generalisation is read from the prequential record (cards'
later matched questions) and from the per-block strict scores of the active
deck; the paired A−B total also contains the fixed deck-mechanics preamble
and the extra learning compute. Verification episodes are extra compute of
the adapted arm and are reported separately (`cost-estimate.json`, group
`verification_actors`). Under the judge's thresholds a card with no effect
passes verification with probability near 5% per proposal, so one or two
ineffective cards over the campaign are expected; the prequential record is
what exposes them. This is one sequential online experiment; a difference
between arms is not attributable to the deck alone, separately from answer
supervision and extra inference. The learner is the same GLM model; no human
or stronger model supplies task-specific critique. There is no resume path:
a campaign stopped mid-block is restarted in a new directory.

## Scoring and evidence

Exactly 100 original prediction records are sealed before final official
scoring; the pinned `reward.py` grades at tolerance 0 (primary) and 0.01
(secondary); empty or failed answers stay in the fixed denominator; no LLM
judge. Reports carry paired results, disagreement counts, per-block strict
scores of the active deck, every card's support/control/prequential outcomes,
served `AGENTS.md` bytes per question, verification episode counts and cost.
