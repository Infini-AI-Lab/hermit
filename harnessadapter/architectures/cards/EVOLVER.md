# Cards evolution contract

What the actor receives: `PREAMBLE.md` followed by every card under `cards/`
that carries an `admitted:` field, compiled by `cards_core/render.py` into at
most `CARDS_MAX` (16,384) bytes. Nothing else in this directory reaches the
actor.

What may change: files under `cards/` only, and only by a controller that has
verified the card. A learner proposes cards; it does not write here. The
verification primitive is a paired re-run of the acting agent: the proposal is
added to a copy of the current deck, the actor is re-run on the questions the
card names as `support` and on control questions the controller picks from the
questions the current deck already gets right, each `k` times. A card is
admitted when the support questions flip to correct (misses allowed over all
support runs = support count − 1, so a single support question must be right
every time), no control question is lost in every run, and the runs do not
cost more than three times the original attempts. The evolver package
`harnessadapter/evolvers/cards/block.py` holds the screen, the judge and the
assembly step as pure functions; the host runs the actor and grades.

What must stay: `adapter.json`, `interface.py`, `cards_core/`, `PREAMBLE.md`,
`README.md` and this file are protected. The card format in
`cards_core/schema.py` is the contract between the learner, the verifier and
the renderer. Editing or removing an admitted card is not supported in this
version; a correction is a new card whose `unless` line narrows the old one.

Evidence the learner sees: the released questions with their submitted and
reference answers, strict grades, trajectories and failure records; the pool of
questions the current deck gets wrong; the ledger of earlier proposals and
their verification outcomes; and, for every admitted card, the graded
questions its scope matched since admission.

What this is not: a proof of generalization. Admission shows that the card
works, once delivered, on the questions it was written for. Whether it helps
later questions is measured afterwards, in the order the questions arrive.
